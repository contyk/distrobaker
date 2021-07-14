import logging
import os
import random
import string
import tempfile
import datetime

import git
import json
import koji
import pyrpkg
import regex
import requests
import yaml

import gi

gi.require_version("Modulemd", "2.0")
from gi.repository import Modulemd  # noqa: E402

# Global logger
logger = logging.getLogger(__name__)

# Global configuration config
c = dict()

# Retry attempts if things fail
retry = 3

# Running in the dry run mode
dry_run = False

# sources file regular expression
sre = regex.compile(
    r"^(?>(?P<hash>[a-f0-9]{32})  (?P<file>.+)|SHA512 \((?P<file>.+)\) = (?<hash>[a-f0-9]{128}))$"
)

# Matching the namespace/component text format
cre = regex.compile(
    r"^(?P<namespace>rpms|modules)/(?P<component>[A-Za-z0-9:._+-]+)$"
)


def loglevel(val=None):
    """Gets or, optionally, sets the logging level of the module.
    Standard numeric levels are accepted.

    :param val: The logging level to use, optional
    :returns: The current logging level
    """
    if val is not None:
        try:
            logger.setLevel(val)
        except ValueError:
            logger.warning(
                "Invalid log level passed to DistroBaker logger: %s", val
            )
        except Exception:
            logger.exception("Unable to set log level: %s", val)
    return logger.getEffectiveLevel()


def retries(val=None):
    """Gets or, optionally, sets the number of retries for various
    operational failures.  Typically used for handling dist-git requests.

    :param val: The number of retries to attept, optional
    :returns: The current value of retries
    """
    global retry
    if val is not None:
        retry = val
    return retry


def pretend(val=None):
    """Gets and, optionally, sets the dry_run mode.

    :param val: True to run in dry_run, False otherwise, optional
    :returns: The current value of the dry_run mode
    """
    global dry_run
    if val is not None:
        dry_run = val
    return dry_run


def get_config():
    """Gets the current global configuration dictionary.

    The dictionary may be empty if no configuration has been successfully
    loaded yet.

    :returns: The global configuration dictionary
    """
    return c


def split_scmurl(scmurl):
    """Splits a `link#ref` style URLs into the link and ref parts.  While
    generic, many code paths in DistroBaker expect these to be branch names.
    `link` forms are also accepted, in which case the returned `ref` is None.

    It also attempts to extract the namespace and component, where applicable.
    These can only be detected if the link matches the standard dist-git
    pattern; in other cases the results may be bogus or None.

    :param scmurl: A link#ref style URL, with #ref being optional
    :returns: A dictionary with `link`, `ref`, `ns` and `comp` keys
    """
    scm = scmurl.split("#", 1)
    nscomp = scm[0].split("/")
    return {
        "link": scm[0],
        "ref": scm[1] if len(scm) >= 2 else None,
        "ns": nscomp[-2] if len(nscomp) >= 2 else None,
        "comp": nscomp[-1] if nscomp else None,
    }


def split_module(comp):
    """Splits modules component name into name and stream pair.  Expects the
    name to be in the `name:stream` format.  Defaults to stream=master if the
    split fails.

    :param comp: The component name
    :returns: Dictionary with name and stream
    """
    ms = comp.split(":")
    return {
        "name": ms[0],
        "stream": ms[1] if len(ms) > 1 and ms[1] else "master",
    }


def parse_sources(comp, ns, sources):
    """Parses the supplied source file and generates a set of
    tuples containing the filename, the hash, and the hashtype.

    :param comps: The component we are parsing
    :param ns: The namespace of the component
    :param sources: The sources file to parse
    :returns: A set of tuples containing the filename, the hash, and the hashtype, or None on error
    """
    src = set()
    try:
        if not os.path.isfile(sources):
            logger.debug("No sources file found for %s/%s.", ns, comp)
            return set()
        with open(sources, "r") as fh:
            for line in fh:
                m = sre.match(line.rstrip())
                if m is None:
                    logger.error(
                        'Cannot parse "%s" from sources of %s/%s.',
                        line,
                        ns,
                        comp,
                    )
                    return None
                m = m.groupdict()
                src.add(
                    (
                        m["file"],
                        m["hash"],
                        "sha512" if len(m["hash"]) == 128 else "md5",
                    )
                )
    except Exception:
        logger.exception("Error processing sources of %s/%s.", ns, comp)
        return None
    logger.debug("Found %d source file(s) for %s/%s.", len(src), ns, comp)
    return src


# FIXME: This needs even more error checking, e.g.
#         - check if blocks are actual dictionaries
#         - check if certain values are what we expect
def load_config(crepo):
    """Loads or updates the global configuration from the provided URL in
    the `link#branch` format.  If no branch is provided, assumes `master`.

    The operation is atomic and the function can be safely called to update
    the configuration without the danger of clobbering the current one.

    `crepo` must be a git repository with `distrobaker.yaml` in it.

    :param crepo: `link#branch` style URL pointing to the configuration
    :returns: The configuration dictionary, or None on error
    """
    global c
    cdir = tempfile.TemporaryDirectory(prefix="distrobaker-")
    logger.info("Fetching configuration from %s to %s", crepo, cdir.name)
    scm = split_scmurl(crepo)
    if scm["ref"] is None:
        scm["ref"] = "master"
    for attempt in range(retry):
        try:
            git.Repo.clone_from(scm["link"], cdir.name).git.checkout(
                scm["ref"]
            )
        except Exception:
            logger.warning(
                "Failed to fetch configuration, retrying (#%d).",
                attempt + 1,
                exc_info=True,
            )
            continue
        else:
            logger.info("Configuration fetched successfully.")
            break
    else:
        logger.error("Failed to fetch configuration, giving up.")
        return None
    if os.path.isfile(os.path.join(cdir.name, "distrobaker.yaml")):
        try:
            with open(os.path.join(cdir.name, "distrobaker.yaml")) as f:
                y = yaml.safe_load(f)
            logger.debug(
                "%s loaded, processing.",
                os.path.join(cdir.name, "distrobaker.yaml"),
            )
        except Exception:
            logger.exception("Could not parse distrobaker.yaml.")
            return None
    else:
        logger.error(
            "Configuration repository does not contain distrobaker.yaml."
        )
        return None
    n = dict()
    if "configuration" in y:
        cnf = y["configuration"]
        for k in ("source", "destination"):
            if k in cnf:
                n[k] = dict()
                if "scm" in cnf[k]:
                    n[k]["scm"] = str(cnf[k]["scm"])
                else:
                    logger.error("Configuration error: %s.scm missing.", k)
                    return None
                if "cache" in cnf[k]:
                    n[k]["cache"] = dict()
                    for kc in ("url", "cgi", "path"):
                        if kc in cnf[k]["cache"]:
                            n[k]["cache"][kc] = str(cnf[k]["cache"][kc])
                        else:
                            logger.error(
                                "Configuration error: %s.cache.%s missing.",
                                k,
                                kc,
                            )
                            return None
                else:
                    logger.error("Configuration error: %s.cache missing.", k)
                    return None
                if "profile" in cnf[k]:
                    n[k]["profile"] = str(cnf[k]["profile"])
                else:
                    logger.error("Configuration error: %s.profile missing.", k)
                    return None
                if "mbs" in cnf[k]:
                    # MBS properties are only relevant for the destination
                    if k == "destination":
                        if not isinstance(cnf[k]["mbs"], dict):
                            logger.error(
                                "Configuration error: %s.mbs must be a mapping.",
                                k,
                            )
                            return None
                        if "auth_method" not in cnf[k]["mbs"]:
                            logger.error(
                                "Configuration error: %s.mbs.%s is missing.",
                                k,
                                "auth_method",
                            )
                            return None
                        mbs_auth_method = str(cnf[k]["mbs"]["auth_method"])
                        mbs_required_configs = ["auth_method", "api_url"]
                        if mbs_auth_method == "oidc":
                            # Try to import this now so the user gets immediate
                            # feedback if it isn't installed
                            try:
                                import openidc_client  # noqa: F401
                            except Exception:
                                logger.exception(
                                    "python-openidc-client needs to be "
                                    "installed for %s.mbs.%s %s",
                                    k,
                                    "auth_method",
                                    mbs_auth_method,
                                )
                                return None
                            mbs_required_configs += [
                                "oidc_id_provider",
                                "oidc_client_id",
                                "oidc_client_secret",
                                "oidc_scopes",
                            ]
                        elif mbs_auth_method == "kerberos":
                            # Try to import this now so the user gets immediate
                            # feedback if it isn't installed
                            try:
                                import requests_kerberos  # noqa: F401
                            except Exception:
                                logger.exception(
                                    "python-requests-kerberos needs to be "
                                    "installed for %s.mbs.%s %s",
                                    k,
                                    "auth_method",
                                    mbs_auth_method,
                                )
                                return None
                        else:
                            logger.error(
                                "Configuration error: %s.mbs.%s %s is unsupported.",
                                k,
                                "auth_method",
                                mbs_auth_method,
                            )
                            return None
                        n[k]["mbs"] = dict()
                        for r in mbs_required_configs:
                            if r not in cnf[k]["mbs"]:
                                logger.error(
                                    "Configuration error: %s.mbs.%s is required when %s is %s.",
                                    k,
                                    r,
                                    "auth_method",
                                    mbs_auth_method,
                                )
                                return None
                            n[k]["mbs"][r] = cnf[k]["mbs"][r]
                    else:
                        logger.warning(
                            "Configuration warning: %s.mbs is extraneous, ignoring.",
                            k,
                        )
                else:
                    # MBS properties required for destination
                    if k == "destination":
                        logger.error("Configuration error: %s.mbs missing.", k)
                        return None
            else:
                logger.error("Configuration error: %s missing.", k)
                return None
        if "trigger" in cnf:
            n["trigger"] = dict()
            for k in ("rpms", "modules"):
                if k in cnf["trigger"]:
                    n["trigger"][k] = str(cnf["trigger"][k])
                else:
                    logger.error("Configuration error: trigger.%s missing.", k)
        else:
            logger.error("Configuration error: trigger missing.")
            return None
        if "build" in cnf:
            n["build"] = dict()
            for k in ("prefix", "target", "platform"):
                if k in cnf["build"]:
                    n["build"][k] = str(cnf["build"][k])
                else:
                    logger.error("Configuration error: build.%s missing.", k)
                    return None
            if "scratch" in cnf["build"]:
                n["build"]["scratch"] = bool(cnf["build"]["scratch"])
            else:
                logger.warning(
                    "Configuration warning: build.scratch not defined, assuming false."
                )
                n["build"]["scratch"] = False
            if ":" not in n["build"]["platform"]:
                logger.error(
                    "Configuration error: build.%s.%s must be in name:stream format.",
                    k,
                    "platform",
                )
                return None
        else:
            logger.error("Configuration error: build missing.")
            return None
        if "git" in cnf:
            n["git"] = dict()
            for k in ("author", "email", "message"):
                if k in cnf["git"]:
                    n["git"][k] = str(cnf["git"][k])
                else:
                    logger.error("Configuration error: git.%s missing.", k)
                    return None
        else:
            logger.error("Configuration error: git missing.")
            return None
        if "control" in cnf:
            n["control"] = dict()
            for k in ("build", "merge", "strict"):
                if k in cnf["control"]:
                    n["control"][k] = bool(cnf["control"][k])
                else:
                    logger.error("Configuration error: control.%s missing.", k)
                    return None
            n["control"]["exclude"] = {"rpms": set(), "modules": set()}
            if "exclude" in cnf["control"]:
                for cns in ("rpms", "modules"):
                    if cns in cnf["control"]["exclude"]:
                        n["control"]["exclude"][cns].update(
                            cnf["control"]["exclude"][cns]
                        )
            for cns in ("rpms", "modules"):
                if n["control"]["exclude"]["rpms"]:
                    logger.info(
                        "Excluding %d component(s) from the %s namespace.",
                        len(n["control"]["exclude"][cns]),
                        cns,
                    )
                else:
                    logger.info(
                        "Not excluding any components from the %s namespace.",
                        cns,
                    )
        else:
            logger.error("Configuration error: control missing.")
            return None
        if "defaults" in cnf:
            n["defaults"] = dict()
            for dk in ("cache", "rpms", "modules"):
                if dk in cnf["defaults"]:
                    n["defaults"][dk] = dict()
                    for dkk in ("source", "destination"):
                        if dkk in cnf["defaults"][dk]:
                            n["defaults"][dk][dkk] = str(
                                cnf["defaults"][dk][dkk]
                            )
                        else:
                            logger.error(
                                "Configuration error: defaults.%s.%s missing.",
                                dk,
                                dkk,
                            )
                else:
                    logger.error(
                        "Configuration error: defaults.%s missing.", dk
                    )
                    return None
            # parse defaults for module sub-components
            for dk in ("rpms",):
                n["defaults"]["modules"][dk] = dict()
                for dkk in ("source", "destination"):
                    if (
                        dk in cnf["defaults"]["modules"]
                        and dkk in cnf["defaults"]["modules"][dk]
                    ):
                        n["defaults"]["modules"][dk][dkk] = cnf["defaults"][
                            "modules"
                        ][dk][dkk]
                    else:
                        logger.warning(
                            "Configuration warning: defaults.modules.%s.%s "
                            "not defined, using value from defaults.%s.%s",
                            dk,
                            dkk,
                            dk,
                            dkk,
                        )
                        n["defaults"]["modules"][dk][dkk] = str(
                            n["defaults"][dk][dkk]
                        )
        else:
            logger.error("Configuration error: defaults missing.")
            return None
    else:
        logger.error("The required configuration block is missing.")
        return None
    components = 0
    nc = {
        "rpms": dict(),
        "modules": dict(),
    }
    if "components" in y:
        cnf = y["components"]
        for k in ("rpms", "modules"):
            if k in cnf:
                for p in cnf[k].keys():
                    components += 1
                    nc[k][p] = dict()
                    cname = p
                    sname = ""
                    if k == "modules":
                        ms = split_module(p)
                        cname = ms["name"]
                        sname = ms["stream"]
                    nc[k][p]["source"] = n["defaults"][k]["source"] % {
                        "component": cname,
                        "stream": sname,
                    }
                    nc[k][p]["destination"] = n["defaults"][k][
                        "destination"
                    ] % {
                        "component": cname,
                        "stream": sname,
                    }
                    nc[k][p]["cache"] = {
                        "source": n["defaults"]["cache"]["source"]
                        % {"component": cname, "stream": sname},
                        "destination": n["defaults"]["cache"]["destination"]
                        % {"component": cname, "stream": sname},
                    }
                    if cnf[k][p] is None:
                        cnf[k][p] = dict()
                    for ck in ("source", "destination"):
                        if ck in cnf[k][p]:
                            nc[k][p][ck] = str(cnf[k][p][ck])
                    if "cache" in cnf[k][p]:
                        for ck in ("source", "destination"):
                            if ck in cnf[k][p]["cache"]:
                                nc[k][p]["cache"][ck] = str(
                                    cnf[k][p]["cache"][ck]
                                )
                    if k == "modules":
                        # parse overrides for module sub-components
                        for cn in ("rpms",):
                            nc[k][p][cn] = dict()
                            if cn in cnf[k][p]:
                                for cp in cnf[k][p][cn].keys():
                                    nc[k][p][cn][cp] = dict()
                                    for ck in ("source", "destination"):
                                        nc[k][p][cn][cp][ck] = n["defaults"][
                                            k
                                        ][cn][ck] % {
                                            "component": cp,
                                            "name": cname,
                                            "ref": "%(ref)s",
                                            "stream": sname,
                                        }
                                        if ck in cnf[k][p][cn][cp]:
                                            nc[k][p][cn][cp][ck] = cnf[k][p][
                                                cn
                                            ][cp][ck]
            logger.info(
                "Found %d configured component(s) in the %s namespace.",
                len(nc[k]),
                k,
            )
    if n["control"]["strict"]:
        logger.info(
            "Running in the strict mode.  Only configured components will be processed."
        )
    else:
        logger.info(
            "Running in the non-strict mode.  All trigger components will be processed."
        )
    if not components:
        if n["control"]["strict"]:
            logger.warning(
                "No components configured while running in the strict mode.  Nothing to do."
            )
        else:
            logger.info("No components explicitly configured.")
    c["main"] = n
    c["comps"] = nc
    return c


def clone_destination_repo(ns, comp, dscm, dirname):
    """Clone the component destination SCM repository to the given directory path.
    Git remote name 'origin' will be used.

    :param ns: The component namespace
    :param comp: The component name
    :param dscm: The destination SCM
    :param dirname: Path to which the requested repository should be cloned
    :returns: repo, or None on error
    """
    logger.debug(
        "Cloning %s/%s from %s#%s",
        ns,
        comp,
        dscm["link"],
        dscm["ref"],
    )
    for attempt in range(retry):
        try:
            repo = git.Repo.clone_from(
                dscm["link"], dirname, branch=dscm["ref"]
            )
        except Exception:
            logger.warning(
                "Cloning attempt #%d/%d failed, retrying.",
                attempt + 1,
                retry,
                exc_info=True,
            )
            continue
        else:
            break
    else:
        logger.error("Exhausted cloning attempts for %s/%s.", ns, comp)
        return None
    logger.debug("Successfully cloned %s/%s.", ns, comp)
    return repo


def fetch_upstream_repo(ns, comp, sscm, repo):
    """Fetch the component source SCM repository to the given git repo.
    Git remote name 'source' will be used.

    :param ns: The component namespace
    :param comp: The component name
    :param sscm: The source SCM
    :param repo: git Repo instance to which the repository should be fetched
    :returns: repo, or None on error
    """
    logger.debug("Fetching upstream repository for %s/%s.", ns, comp)
    if sscm["ref"]:
        logger.debug(
            "Fetching the %s upstream branch for %s/%s.", sscm["ref"], ns, comp
        )
    else:
        logger.debug("Fetching all upstream branches for %s/%s.", ns, comp)
    repo.git.remote("add", "source", sscm["link"])
    for attempt in range(retry):
        try:
            if sscm["ref"]:
                repo.git.fetch("--tags", "source", sscm["ref"])
            else:
                repo.git.fetch("--tags", "--all")
        except Exception:
            logger.warning(
                "Fetching upstream attempt #%d/%d failed, retrying.",
                attempt + 1,
                retry,
                exc_info=True,
            )
            continue
        else:
            break
    else:
        logger.error(
            "Exhausted upstream fetching attempts for %s/%s.", ns, comp
        )
        return None
    logger.debug(
        "Successfully fetched upstream repository for %s/%s.", ns, comp
    )
    return repo


def configure_repo(ns, comp, repo):
    """Configure given git repo.

    :param ns: The component namespace
    :param comp: The component name
    :param repo: git Repo instance for the repository to be configured
    :returns: repo, or None on error
    """
    logger.debug("Configuring repository properties for %s/%s.", ns, comp)
    try:
        repo.git.config("user.name", c["main"]["git"]["author"])
        repo.git.config("user.email", c["main"]["git"]["email"])
    except Exception:
        logger.exception(
            "Failed configuring the git repository while processing %s/%s.",
            ns,
            comp,
        )
        return None
    logger.debug(
        "Sucessfully configured repository properties for %s/%s.", ns, comp
    )
    return repo


def sync_repo_merge(ns, comp, repo, bscm, sscm, dscm):
    """Synchronize component repo source branch into the desination branch using
    the merge mechanism.

    Does not push the repo.

    :param ns: The component namespace
    :param comp: The component name
    :param repo: git Repo instance to be synchronized
    :param bscm: The component build SCM
    :param sscm: The source SCM
    :param dscm: The destination SCM
    :returns: repo, or None on error
    """
    logger.debug(
        "Attempting to synchronize the %s/%s branches using the merge mechanism.",
        ns,
        comp,
    )
    logger.debug(
        "Generating a temporary merge branch name for %s/%s.", ns, comp
    )
    for attempt in range(retry):
        bname = "".join(random.choice(string.ascii_letters) for i in range(16))
        logger.debug("Checking the availability of %s/%s#%s.", ns, comp, bname)
        try:
            repo.git.rev_parse("--quiet", bname, "--")
            logger.debug(
                "%s/%s#%s is taken.  Some people choose really weird branch names.  "
                "Retrying, attempt #%d/%d.",
                ns,
                comp,
                bname,
                attempt + 1,
                retry,
            )
        except Exception:
            logger.debug(
                "Using %s/%s#%s as the temporary merge branch name.",
                ns,
                comp,
                bname,
            )
            break
    else:
        logger.error(
            "Exhausted attempts finding an unused branch name while synchronizing %s/%s; "
            "this is very rare, congratulations.",
            ns,
            comp,
        )
        return None

    logger.debug(
        "Locating build branch reference for %s/%s.",
        ns,
        comp,
    )
    # if syncing a named branch present in both source and destination, make
    # sure we merge from the source; otherwise it's likely a commit hash
    for bref in ("source/{}".format(bscm["ref"]), bscm["ref"]):
        try:
            repo.git.rev_parse("--quiet", bref, "--")
            break
        except Exception:
            continue
    else:
        logger.error(
            "Cannot locate build branch reference while synchronizing %s/%s.",
            ns,
            comp,
        )
        return None
    logger.debug(
        "Using build branch reference %s while synchronizing %s/%s.",
        bref,
        ns,
        comp,
    )

    try:
        actor = "{} <{}>".format(
            c["main"]["git"]["author"], c["main"]["git"]["email"]
        )
        repo.git.checkout(bref)
        repo.git.switch("-c", bname)
        repo.git.merge(
            "--allow-unrelated-histories",
            "--no-commit",
            "-s",
            "ours",
            dscm["ref"],
        )
        repo.git.commit(
            "--author",
            actor,
            "--allow-empty",
            "-m",
            "Temporary working tree merge",
        )
        repo.git.checkout(dscm["ref"])
        repo.git.merge("--no-commit", "--squash", bname)
        msg = "{}\nSource: {}#{}".format(
            c["main"]["git"]["message"], sscm["link"], bscm["ref"]
        )
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="msg-{}-{}-".format(ns, comp)
        ) as msgfile:
            msgfile.write(msg)
            msgfile.flush()
            repo.git.commit(
                "--author", actor, "--allow-empty", "-F", msgfile.name
            )
    except Exception:
        logger.exception("Failed to merge %s/%s.", ns, comp)
        return None
    logger.debug("Successfully merged %s/%s with upstream.", ns, comp)
    return repo


def sync_repo_pull(ns, comp, repo, bscm):
    """Synchronize component repo source branch into the desination branch using
    the clean pull mechanism. Branches must be compatible.

    Does not push the repo.

    :param ns: The component namespace
    :param comp: The component name
    :param repo: git Repo instance to be synchronized
    :param bscm: The component build SCM
    :returns: repo, or None on error
    """
    logger.debug(
        "Attempting to synchronize the %s/%s branches using the clean pull mechanism.",
        ns,
        comp,
    )
    try:
        repo.git.pull("--ff-only", "--tags", "source", bscm["ref"])
    except Exception:
        logger.exception(
            "Failed to perform a clean pull for %s/%s, skipping.", ns, comp
        )
        return None
    logger.debug("Successfully pulled %s/%s from upstream.", ns, comp)
    return repo


def repo_push(ns, comp, repo, dscm):
    """Push synchronized repo to component destination SCM repository

    :param ns: The component namespace
    :param comp: The component name
    :param repo: git Repo instance to be synchronized
    :param dscm: The destination SCM
    :returns: repo, or None on error
    """
    logger.debug("Pushing synchronized contents for %s/%s.", ns, comp)

    for attempt in range(retry):
        try:
            if not dry_run:
                logger.debug("Pushing %s/%s.", ns, comp)
                repo.git.push(
                    "--tags", "--set-upstream", "origin", dscm["ref"]
                )
                logger.debug("Successfully pushed %s/%s.", ns, comp)
            else:
                logger.debug("Pushing %s/%s (--dry-run).", ns, comp)
                repo.git.push(
                    "--dry-run",
                    "--tags",
                    "--set-upstream",
                    "origin",
                    dscm["ref"],
                )
                logger.debug(
                    "Successfully pushed %s/%s (--dry-run).", ns, comp
                )
        except Exception:
            logger.warning(
                "Pushing attempt #%d/%d failed, retrying.",
                attempt + 1,
                retry,
                exc_info=True,
            )
            continue
        else:
            return repo
    else:
        logger.error("Exhausted pushing attempts for %s/%s.", ns, comp)
        return None


def sync_module_components(comp, nvr, modulemd=None):
    """Synchronizes the SCM repositories for the components of the given module.

    :param comp: The modular component name
    :param nvr: NVR of module to synchronize
    :param modulemd: Optional modulemd for module from build system
    :returns: True if successful, or False on error
    """
    logger.debug("Synchronizing components for module %s: %s", comp, nvr)

    if "main" not in c:
        logger.critical("DistroBaker is not configured, aborting.")
        return False
    if nvr is None:
        logger.error(
            "NVR not specified for module %s",
            comp,
        )
        return False
    if modulemd is None:
        logger.debug(
            "Retrieving modulemd for module %s: %s",
            comp,
            nvr,
        )
        bsys = get_buildsys("source")
        if bsys is None:
            logger.error(
                "Build system unavailable, cannot retrieve the module info for %s.",
                nvr,
            )
            return False
        try:
            bsrc = bsys.getBuild(nvr)
        except Exception:
            logger.exception(
                "An error occured while retrieving the module info for %s.",
                nvr,
            )
            return False
        try:
            minfo = bsrc["extra"]["typeinfo"]["module"]
            modulemd = minfo["modulemd_str"]
        except Exception:
            logger.error("Cannot retrieve module info for %s.", nvr)
            return False
        logger.debug("Modulemd for %s: %s", nvr, modulemd)

    mmd = Modulemd.read_packager_string(modulemd)
    if not isinstance(mmd, Modulemd.ModuleStreamV2):
        logger.error(
            "Unable to parse module metadata string for %s: %s", nvr, modulemd
        )
        return False

    gitdirs = dict(rpms=dict(), modules=dict())
    scmurls = dict(rpms=dict(), modules=dict())

    mcomps = mmd.get_rpm_component_names()
    logger.debug("Module has %d RPM components", len(mcomps))
    for mc in mcomps:
        logger.debug("RPM component: %s", mc)
        compinfo = mmd.get_rpm_component(mc)
        crepo = compinfo.get_repository()
        ccache = compinfo.get_cache()
        cref = compinfo.get_ref()
        # TODO: do we actually need the ref MBS stored in the xmd?
        try:
            xref = mmd.get_xmd()["mbs"]["rpms"][mc]["ref"]
        except Exception:
            xref = None
        logger.debug("  repo: %s", crepo)
        logger.debug("  cache: %s", ccache)
        logger.debug("  ref: %s", cref)
        logger.debug("  xmd ref: %s", xref)
        gitdirs["rpms"][mc] = tempfile.TemporaryDirectory(
            prefix="mcrepo-{}-{}-{}-".format(comp, "rpms", mc)
        )
        logger.debug(
            "Temporary directory created: %s", gitdirs["rpms"][mc].name
        )

        if cref is not None:
            cscmurl = "{}#{}".format(crepo, cref)
        else:
            cscmurl = crepo

        scmurls["rpms"][mc] = sync_repo(
            mc,
            "rpms",
            gitdir=gitdirs["rpms"][mc].name,
            cmodule=comp,
            scmurl=cscmurl,
            bcache=ccache,
        )
        if scmurls["rpms"][mc] is None:
            logger.error(
                "Synchronization of component %s/%s failed, aborting module sync.",
                "rpms",
                mc,
            )
            return False

    mcomps = mmd.get_module_component_names()
    logger.debug("Module has %d module components", len(mcomps))
    for mc in mcomps:
        logger.debug("Module component: %s", mc)
        compinfo = mmd.get_module_component(mc)
        crepo = compinfo.get_repository()
        cref = compinfo.get_ref()
        # TODO: do we actually need the ref MBS stored in the xmd?
        # does it even exist for bundled modules?
        try:
            xref = mmd.get_xmd()["mbs"]["modules"][mc]["ref"]
        except Exception:
            xref = None
        logger.debug("  repo: %s", crepo)
        logger.debug("  ref: %s", cref)
        logger.debug("  xmd ref: %s", xref)
        gitdirs["modules"][mc] = tempfile.TemporaryDirectory(
            prefix="mcrepo-{}-{}-{}-".format(comp, "modules", mc)
        )
        logger.debug(
            "Temporary directory created: %s", gitdirs["modules"][mc].name
        )
        # TODO some day: implement syncing of module component repos
        scmurls["modules"][mc] = None  # sync_repo(...)
        logger.critical(
            "Module %s: synchronization not yet implemented for component module %s, aborting.",
            comp,
            mc,
        )
        return True

    # if all the component syncs succeeded, push all the repos
    for ns in ("rpms", "modules"):
        for mc in gitdirs[ns].keys():
            repo = git.Repo(gitdirs[ns][mc].name)
            dscm = split_scmurl(scmurls[ns][mc])
            if repo_push(ns, mc, repo, dscm) is None:
                logger.error(
                    "Module %s: failed to push component %s/%s, skipping.",
                    comp,
                    ns,
                    mc,
                )
                return False

    return True


def sync_repo(
    comp,
    ns="rpms",
    nvr=None,
    gitdir=None,
    cmodule=None,
    scmurl=None,
    bcache=None,
):
    """Synchronizes the component SCM repository for the given NVR.
    If no NVR is provided, finds the latest build in the corresponding
    trigger tag.

    Calls sync_cache() if required.  Does not call build_comp().

    :param comp: The component name.
    :param ns: The component namespace.
    :param nvr: Optional NVR to synchronize.
    :param gitdir: Optional empty directory to use as git repository directory
    which will NOT be pushed. If not specified, a temporary git repository
    directory will be created and the repository WILL be pushed.
    :param cmodule: Optional containing module name:stream. Must be specified
    if and only if syncing module RPM components.
    :param scmurl: Optional URL of custom source component repository. Must be
    specified if and only if syncing module RPM components. If not specified, the
    configuration default location is used.
    :param bcache: Optional URL of custom source lookaside cache. If not
    specified, the configuration default location is used.
    :returns: The desination SCM URL of the final synchronized commit (if
    gitdir not specified) or branch to push to (if gitdir provided). None on
    error.
    """
    if "main" not in c:
        logger.critical("DistroBaker is not configured, aborting.")
        return None
    if comp in c["main"]["control"]["exclude"][ns]:
        logger.critical(
            "The component %s/%s is excluded from sync, aborting.", ns, comp
        )
        return None

    logger.info("Synchronizing SCM for %s/%s.", ns, comp)

    if scmurl:
        bscmurl = scmurl
        bmmd = None
    else:
        nvr = nvr if nvr else get_build(comp, ns=ns)
        if nvr is None:
            logger.error(
                "NVR not specified and no builds for %s/%s could be found, skipping.",
                ns,
                comp,
            )
            return None
        binfo = get_build_info(nvr)
        if binfo is None:
            logger.error(
                "Could not find build SCMURL for %s/%s: %s, skipping.",
                ns,
                comp,
                nvr,
            )
            return None
        bscmurl = binfo["scmurl"]
        bmmd = binfo["modulemd"]

    bscm = split_scmurl(bscmurl)
    bscm["ref"] = bscm["ref"] if bscm["ref"] else "master"

    if scmurl:
        if bscm["link"] != c["main"]["source"]["scm"]:
            # TODO: this never matches; make the check useful
            logger.warning(
                "The custom source SCM URL for %s/%s (%s) doesn't match "
                "configuration (%s), ignoring.",
                ns,
                comp,
                bscm["link"],
                c["main"]["source"]["scm"],
            )

    if cmodule and nvr is None:
        # when syncing module RPM components, assign a dummy nvr in comp:branch
        # format that is used only for log messages
        nvr = "{}:{}".format(comp, bscm["ref"])

    logger.debug("Processing %s/%s: %s", ns, comp, nvr)

    logger.debug("Build SCMURL for %s/%s: %s", ns, comp, bscmurl)

    if ns == "modules":
        ms = split_module(comp)
        cname = ms["name"]
        sname = ms["stream"]
    else:
        cname = comp
        sname = ""

    if cmodule:
        if ns == "modules":
            logger.critical(
                "Synchronizing module subcomponent (%s/%s) of module (%s) is not yet supported.",
                ns,
                comp,
                cmodule,
            )
            return None

        # check for module subcomponent overrides
        if (
            cmodule in c["comps"]["modules"]
            and comp in c["comps"]["modules"][cmodule][ns]
        ):
            csrc = c["comps"]["modules"][cmodule][ns][comp]["source"]
            cdst = c["comps"]["modules"][cmodule][ns][comp]["destination"]
        else:
            csrc = c["main"]["defaults"]["modules"][ns]["source"]
            cdst = c["main"]["defaults"]["modules"][ns]["destination"]

        # append #ref if not already present
        if "#" not in csrc:
            csrc += "#%(ref)s"
        if "#" not in cdst:
            cdst += "#%(ref)s"

        # when syncing module RPM components, ref is the stream branch from build
        # TODO: do we also need a mapped key for the component's stream?
        cms = split_module(cmodule)
        csrc = csrc % {
            "component": cname,
            "name": cms["name"],
            "ref": bscm["ref"],
            "stream": cms["stream"],
        }
        cdst = cdst % {
            "component": cname,
            "name": cms["name"],
            "ref": bscm["ref"],
            "stream": cms["stream"],
        }
    elif comp in c["comps"][ns]:
        csrc = c["comps"][ns][comp]["source"]
        cdst = c["comps"][ns][comp]["destination"]
    else:
        csrc = c["main"]["defaults"][ns]["source"] % {
            "component": cname,
            "stream": sname,
        }
        cdst = c["main"]["defaults"][ns]["destination"] % {
            "component": cname,
            "stream": sname,
        }
    sscm = split_scmurl(
        "{}/{}/{}".format(c["main"]["source"]["scm"], ns, csrc)
    )
    dscm = split_scmurl(
        "{}/{}/{}".format(c["main"]["destination"]["scm"], ns, cdst)
    )
    dscm["ref"] = dscm["ref"] if dscm["ref"] else "master"

    if gitdir:
        # if a git repo directory was provided, don't do pushes since they
        # will be handled by the caller
        pushrepo = False
    else:
        tempdir = tempfile.TemporaryDirectory(
            prefix="repo-{}-{}-".format(ns, comp)
        )
        logger.debug("Temporary directory created: %s", tempdir.name)
        gitdir = tempdir.name
        pushrepo = True
    logger.debug("Using git repository directory: %s", gitdir)

    repo = clone_destination_repo(ns, comp, dscm, gitdir)
    if repo is None:
        logger.error(
            "Failed to clone destination repo for %s/%s, skipping.", ns, comp
        )
        return None

    if fetch_upstream_repo(ns, comp, sscm, repo) is None:
        logger.error(
            "Failed to fetch upstream repo for %s/%s, skipping.", ns, comp
        )
        return None

    if configure_repo(ns, comp, repo) is None:
        logger.error(
            "Failed to configure the git repository for %s/%s, skipping.",
            ns,
            comp,
        )
        return None

    logger.debug("Gathering destination files for %s/%s.", ns, comp)

    dsrc = parse_sources(comp, ns, os.path.join(repo.working_dir, "sources"))
    if dsrc is None:
        logger.error(
            "Error processing the %s/%s destination sources file, skipping.",
            ns,
            comp,
        )
        return None

    if c["main"]["control"]["merge"]:
        if sync_repo_merge(ns, comp, repo, bscm, sscm, dscm) is None:
            logger.error(
                "Failed to sync merge repo for %s/%s, skipping.", ns, comp
            )
            return None
    else:
        if sync_repo_pull(ns, comp, repo, bscm) is None:
            logger.error(
                "Failed to sync pull repo for %s/%s, skipping.", ns, comp
            )
            return None

    logger.debug("Gathering source files for %s/%s.", ns, comp)
    ssrc = parse_sources(comp, ns, os.path.join(repo.working_dir, "sources"))
    if ssrc is None:
        logger.error(
            "Error processing the %s/%s source sources file, skipping.",
            ns,
            comp,
        )
        return None

    srcdiff = ssrc - dsrc
    if srcdiff:
        logger.debug("Source files for %s/%s differ.", ns, comp)
        if sync_cache(comp, srcdiff, ns, scacheurl=bcache) is None:
            logger.error(
                "Failed to synchronize sources for %s/%s, skipping.", ns, comp
            )
            return None
    else:
        logger.debug("Source files for %s/%s are up-to-date.", ns, comp)

    logger.debug("Component %s/%s successfully synchronized.", ns, comp)

    if ns == "modules":
        if not sync_module_components(comp, nvr, bmmd):
            logger.error(
                "Failed to sync module components for %s/%s, skipping.",
                ns,
                comp,
            )
            return None

    if pushrepo:
        if repo_push(ns, comp, repo, dscm) is None:
            logger.error("Failed to push %s/%s, skipping.", ns, comp)
            return None
        logger.info("Successfully synchronized %s/%s.", ns, comp)
        return "{}#{}".format(dscm["link"], repo.git.rev_parse("HEAD"))
    else:
        logger.info("Successfully synchronized %s/%s without push", ns, comp)
        return "{}#{}".format(dscm["link"], dscm["ref"])


def sync_cache(comp, sources, ns="rpms", scacheurl=None):
    """Synchronizes lookaside cache contents for the given component.
    Expects a set of (filename, hash, hastype) tuples to synchronize, as
    returned by parse_sources().

    :param comp: The component name
    :param sources: The set of source tuples
    :param ns: The component namespace
    :param scacheurl: Optional source lookaside cache url for modular RPM
    components
    :returns: The number of files processed, or None on error
    """
    if "main" not in c:
        logger.critical("DistroBaker is not configured, aborting.")
        return None
    if comp in c["main"]["control"]["exclude"][ns]:
        logger.critical(
            "The component %s/%s is excluded from sync, aborting.", ns, comp
        )
        return None
    logger.debug(
        "Synchronizing %d cache file(s) for %s/%s.", len(sources), ns, comp
    )
    if scacheurl:
        if scacheurl != c["main"]["source"]["cache"]["url"]:
            logger.warning(
                "The custom source lookaside cache URL for %s/%s (%s) doesn't "
                "match configuration (%s), ignoring.",
                ns,
                comp,
                scacheurl,
                c["main"]["source"]["cache"]["url"],
            )
    scache = pyrpkg.lookaside.CGILookasideCache(
        "sha512",
        c["main"]["source"]["cache"]["url"],
        c["main"]["source"]["cache"]["cgi"],
    )
    scache.download_path = c["main"]["source"]["cache"]["path"]
    dcache = pyrpkg.lookaside.CGILookasideCache(
        "sha512",
        c["main"]["destination"]["cache"]["url"],
        c["main"]["destination"]["cache"]["cgi"],
    )
    dcache.download_path = c["main"]["destination"]["cache"]["path"]
    tempdir = tempfile.TemporaryDirectory(
        prefix="cache-{}-{}-".format(ns, comp)
    )
    logger.debug("Temporary directory created: %s", tempdir.name)
    if comp in c["comps"][ns]:
        scname = c["comps"][ns][comp]["cache"]["source"]
        dcname = c["comps"][ns][comp]["cache"]["destination"]
    else:
        scname = c["main"]["defaults"]["cache"]["source"] % {"component": comp}
        dcname = c["main"]["defaults"]["cache"]["source"] % {"component": comp}
    for s in sources:
        # There's no API for this and .upload doesn't let us override it
        dcache.hashtype = s[2]
        for attempt in range(retry):
            try:
                if not dcache.remote_file_exists(
                    "{}/{}".format(ns, dcname), s[0], s[1]
                ):
                    logger.debug(
                        "File %s for %s/%s (%s/%s) not available in the "
                        "destination cache, downloading.",
                        s[0],
                        ns,
                        comp,
                        ns,
                        dcname,
                    )
                    scache.download(
                        "{}/{}".format(ns, scname),
                        s[0],
                        s[1],
                        os.path.join(tempdir.name, s[0]),
                        hashtype=s[2],
                    )
                    logger.debug(
                        "File %s for %s/%s (%s/%s) successfully downloaded.  "
                        "Uploading to the destination cache.",
                        s[0],
                        ns,
                        comp,
                        ns,
                        scname,
                    )
                    if not dry_run:
                        dcache.upload(
                            "{}/{}".format(ns, dcname),
                            os.path.join(tempdir.name, s[0]),
                            s[1],
                        )
                        logger.debug(
                            "File %s for %s/%s (%s/%s) )successfully uploaded "
                            "to the destination cache.",
                            s[0],
                            ns,
                            comp,
                            ns,
                            dcname,
                        )
                    else:
                        logger.debug(
                            "Running in dry run mode, not uploading %s for %s/%s.",
                            s[0],
                            ns,
                            comp,
                        )
                else:
                    logger.debug(
                        "File %s for %s/%s (%s/%s) already uploaded, skipping.",
                        s[0],
                        ns,
                        comp,
                        ns,
                        dcname,
                    )
            except Exception:
                logger.warning(
                    "Failed attempt #%d/%d handling %s for %s/%s (%s/%s -> %s/%s), retrying.",
                    attempt + 1,
                    retry,
                    s[0],
                    ns,
                    comp,
                    ns,
                    scname,
                    ns,
                    dcname,
                    exc_info=True,
                )
            else:
                break
        else:
            logger.error(
                "Exhausted lookaside cache synchronization attempts for %s/%s "
                "while working on %s, skipping.",
                ns,
                comp,
                s[0],
            )
            return None
    return len(sources)


def build_comp(comp, ref, ns="rpms"):
    """Submits a build for the requested component.  Requires the
    component name, namespace and the destination SCM reference to build.
    The build is submitted for the configured build target.  The build
    SCMURL is prefixed with the configured prefix.

    In the dry-run mode, the returned task ID is 0.

    :param comp: The component name
    :param ref: The SCM reference
    :param ns: The component namespace
    :returns: The build system task ID for RPMS, the module build ID for
    modules, or None on error
    """
    if "main" not in c:
        logger.critical("DistroBaker is not configured, aborting.")
        return None
    if comp in c["main"]["control"]["exclude"][ns]:
        logger.critical(
            "The component %s/%s is excluded from sync, aborting.", ns, comp
        )
        return None
    if not c["main"]["control"]["build"]:
        logger.critical("Builds are disabled, aborting.")
        return None
    logger.info("Processing build for %s/%s.", ns, comp)
    if not dry_run:
        bsys = get_buildsys("destination")
    buildcomp = comp
    if comp in c["comps"][ns]:
        buildcomp = split_scmurl(c["comps"][ns][comp]["destination"])["comp"]
    if ns == "rpms":
        buildscmurl = "{}/{}/{}#{}".format(
            c["main"]["build"]["prefix"], ns, buildcomp, ref
        )
        try:
            if not dry_run:
                task = bsys.build(
                    buildscmurl,
                    c["main"]["build"]["target"],
                    {"scratch": c["main"]["build"]["scratch"]},
                )
                logger.debug(
                    "Build submitted for %s/%s; task %d; SCMURL: %s.",
                    ns,
                    comp,
                    task,
                    buildscmurl,
                )
            else:
                task = 0
                logger.info(
                    "Running in the dry mode, not submitting any builds for %s/%s (%s).",
                    ns,
                    comp,
                    buildscmurl,
                )
            return task
        except Exception:
            logger.exception(
                "Failed submitting build for %s/%s (%s).",
                ns,
                comp,
                buildscmurl,
            )
            return None
    elif ns == "modules":
        ms = split_module(buildcomp)
        buildscmurl = "{}/{}/{}#{}".format(
            c["main"]["build"]["prefix"], ns, ms["name"], ref
        )
        ps = split_module(c["main"]["build"]["platform"])
        body = {
            "scmurl": buildscmurl,
            "branch": ms["stream"],
            "buildrequire_overrides": {ps["name"]: [ps["stream"]]},
            "scratch": c["main"]["build"]["scratch"],
        }
        request_url = "{}/{}/".format(
            c["main"]["destination"]["mbs"]["api_url"], "module-builds"
        )
        logger.debug(
            "Body of build request for %s/%s to POST to %s using auth_method %s: %s",
            ns,
            comp,
            request_url,
            c["main"]["destination"]["mbs"]["auth_method"],
            body,
        )

        if not dry_run:
            if c["main"]["destination"]["mbs"]["auth_method"] == "kerberos":
                try:
                    import requests_kerberos

                    data = json.dumps(body)
                    auth = requests_kerberos.HTTPKerberosAuth(
                        mutual_authentication=requests_kerberos.OPTIONAL,
                    )
                    resp = requests.post(request_url, data=data, auth=auth)
                except Exception:
                    logger.exception(
                        "Failed submitting build for %s/%s (%s).",
                        ns,
                        comp,
                        buildscmurl,
                    )
                    return None

            elif c["main"]["destination"]["mbs"]["auth_method"] == "oidc":
                try:
                    import openidc_client

                    mapping = {
                        "Token": "Token",
                        "Authorization": "Authorization",
                    }
                    # Get the auth token using the OpenID client
                    oidc = openidc_client.OpenIDCClient(
                        "mbs_build",
                        c["main"]["destination"]["mbs"]["oidc_id_provider"],
                        mapping,
                        c["main"]["destination"]["mbs"]["oidc_client_id"],
                        c["main"]["destination"]["mbs"]["oidc_client_secret"],
                    )
                    resp = oidc.send_request(
                        request_url,
                        http_method="POST",
                        json=body,
                        scopes=c["main"]["destination"]["mbs"]["oidc_scopes"],
                    )
                except Exception:
                    logger.exception(
                        "Failed submitting build for %s/%s (%s).",
                        ns,
                        comp,
                        buildscmurl,
                    )
                    return None
            else:
                logger.critical(
                    "Cannot build %s/%s; unknown auth_method: %s",
                    ns,
                    comp,
                    c["main"]["destination"]["mbs"]["auth_method"],
                )
                return None

            logger.debug(
                "Build request for %s/%s (%s) returned status %d.",
                ns,
                comp,
                buildscmurl,
                resp.status_code,
            )
            if resp.status_code == 401:
                logger.critical(
                    "Cannot build %s/%s: MBS authentication failed using auth_method %s. "
                    "Make sure you have a valid ticket/token.",
                    ns,
                    comp,
                    c["main"]["destination"]["mbs"]["auth_method"],
                )
                return None
            elif not resp.ok:
                logger.critical(
                    "Cannot build %s/%s: request failed with: %s",
                    ns,
                    comp,
                    resp.text,
                )
                return None

            rdata = resp.json()
            build = rdata[0] if isinstance(rdata, list) else rdata
            buildid = build["id"]
            logger.debug(
                "Build submitted for %s/%s; buildid %d; SCMURL: %s.",
                ns,
                comp,
                buildid,
                buildscmurl,
            )
            return buildid

        else:
            logger.info(
                "Running in the dry mode, not submitting any builds for %s/%s (%s).",
                ns,
                comp,
                buildscmurl,
            )
            return 0
    else:
        logger.critical("Cannot build %s/%s; unknown namespace.", ns, comp)
        return None


def process_message(msg):
    """Processes a fedora-messaging message.  We can only handle Koji
    tagging events; messaging should be configured properly.

    If the message is recognized and matches our configuration or mode,
    the function calls `sync_repo()` and `build_comp()`.

    :param msg: fedora-messaging message
    :returns: None
    """
    if "main" not in c:
        logger.critical("DistroBaker is not configured, aborting.")
        return None
    logger.debug("Received a message with topic %s.", msg.topic)
    if msg.topic.endswith("buildsys.tag"):
        try:
            logger.debug("Processing a tagging event message.")
            comp = msg.body["name"]
            nvr = "{}-{}-{}".format(
                msg.body["name"], msg.body["version"], msg.body["release"]
            )
            tag = msg.body["tag"]
            logger.debug("Tagging event for %s, tag %s received.", comp, tag)
        except Exception:
            logger.exception("Failed to process the message: %s", msg)
            return None

        if tag == c["main"]["trigger"]["rpms"]:
            ns = "rpms"
            logger.debug(
                "Message tag configured as an RPM trigger, processing."
            )
        elif tag == c["main"]["trigger"]["modules"]:
            ns = "modules"
            logger.debug(
                "Message tag configured as a Module trigger, processing."
            )
            # get un-mangled name:stream for nvr
            binfo = get_build_info(nvr)
            if (
                binfo is None
                or binfo["name"] is None
                or binfo["stream"] is None
            ):
                logger.error(
                    "Could not retrieve module build info for %s, skipping.",
                    nvr,
                )
                return None
            bcomp = "{}:{}".format(binfo["name"], binfo["stream"])
            if comp != bcomp:
                logger.debug(
                    "Using unmangled component name: %s",
                    bcomp,
                )
                comp = bcomp
            # get SCM component name, stripped of any .git and ? suffixes
            scm_comp = regex.sub(
                r"(\.git)?\??$", "", split_scmurl(binfo["scmurl"])["comp"]
            )
            # skip generated *-devel modules
            if binfo["name"] != scm_comp:
                logger.info(
                    "Module name %s does not match SCM component name %s, skipping.",
                    binfo["name"],
                    scm_comp,
                )
                return None
        else:
            logger.debug("Message tag not configured as a trigger, ignoring.")
            return None

        if comp in c["comps"][ns] or not c["main"]["control"]["strict"]:
            logger.info("Handling trigger for %s/%s, tag %s.", ns, comp, tag)
            if comp in c["main"]["control"]["exclude"][ns]:
                logger.info(
                    "The %s/%s component is excluded from sync, skipping.",
                    ns,
                    comp,
                )
                return None
            scmurl = sync_repo(comp, ns=ns, nvr=nvr)
            if scmurl is not None:
                if c["main"]["control"]["build"]:
                    scm = split_scmurl(scmurl)
                    task = build_comp(comp, scm["ref"], ns=ns)
                    if task is not None:
                        logger.info(
                            "Build submission of %s/%s complete, task %s, trigger processed.",
                            ns,
                            comp,
                            task,
                        )
                    else:
                        logger.error(
                            "Build submission of %s/%s failed, aborting trigger.",
                            ns,
                            comp,
                        )
                else:
                    logger.info(
                        "Builds are disabled, no build attempted for %s/%s, trigger processed.",
                        ns,
                        comp,
                    )
            else:
                logger.error(
                    "Synchronization of %s/%s failed, aborting trigger.",
                    ns,
                    comp,
                )
        else:
            logger.debug(
                "Component %s/%s not configured for sync and the strict "
                "mode is enabled, ignoring.",
                ns,
                comp,
            )
    else:
        logger.warning("Unable to handle %s topics, ignoring.", msg.topic)
    return None


def process_components(compset):
    """Processes the supplied set of components.  If the set is empty,
    fetch all latest components from the trigger tags.

    :param compset: A set of components to process in the `ns/comp` form
    :returns: None
    """
    if not isinstance(compset, set):
        logger.critical("process_components() must be passed a set.")
        return None
    if "main" not in c:
        logger.critical("DistroBaker is not configured, aborting.")
        return None

    # Generate a dictionary (key module:stream, value nvr) of all of the latest
    # modular builds for the source tag.
    # Note: Querying tagged modules with latest=True only returns the latest
    # module tagged by name without regard to stream. So, we need need to query
    # everything to figure out the latest per stream ourselves. It helps that
    # the list returned by Koji is ordered so the most recently tagged builds
    # are at the end of the list. This also fetches and uses the actual
    # un-mangled name and stream for each module.
    latest = dict()
    for x in get_buildsys("source").listTagged(
        c["main"]["trigger"]["modules"],
    ):
        binfo = get_build_info(x["nvr"])
        if binfo is None or binfo["name"] is None or binfo["stream"] is None:
            logger.error(
                "Could not get module info for %s, skipping.",
                x["nvr"],
            )
        else:
            latest["{}:{}".format(binfo["name"], binfo["stream"])] = x["nvr"]

    if not compset:
        logger.debug(
            "No components selected, gathering components from triggers."
        )
        compset.update(
            "{}/{}".format("rpms", x["package_name"])
            for x in get_buildsys("source").listTagged(
                c["main"]["trigger"]["rpms"], latest=True
            )
        )
        compset.update("{}/{}".format("modules", x) for x in latest.keys())
    logger.info("Processing %d component(s).", len(compset))

    processed = 0
    for rec in sorted(compset, key=str.lower):
        m = cre.match(rec)
        if m is None:
            logger.error("Cannot process %s; looks like garbage.", rec)
            continue
        m = m.groupdict()
        logger.info("Processing %s.", rec)
        if m["component"] in c["main"]["control"]["exclude"][m["namespace"]]:
            logger.info(
                "The %s/%s component is excluded from sync, skipping.",
                m["namespace"],
                m["component"],
            )
            continue
        if (
            c["main"]["control"]["strict"]
            and m["component"] not in c["comps"][m["namespace"]]
        ):
            logger.info(
                "The %s/%s component not configured while the strict mode is enabled, ignoring.",
                m["namespace"],
                m["component"],
            )
            continue
        scmurl = sync_repo(
            comp=m["component"],
            ns=m["namespace"],
            nvr=latest.get(m["component"]),
        )
        if scmurl is not None:
            scm = split_scmurl(scmurl)
            build_comp(comp=m["component"], ref=scm["ref"], ns=m["namespace"])
        logger.info("Done processing %s.", rec)
        processed += 1
    logger.info(
        "Synchronized %d component(s), %d skipped.",
        processed,
        len(compset) - processed,
    )
    return None


def get_build_info(nvr):
    """Get SCMURL, plus extra attributes for modules, for a source build system
    build NVR.  NVRs are unique.

    :param nvr: The build NVR to look up
    :returns: A dictionary with `scmurl`, `name`, `stream`, and `modulemd` keys,
    or None on error
    """
    if "main" not in c:
        logger.critical("DistroBaker is not configured, aborting.")
        return None
    bsys = get_buildsys("source")
    if bsys is None:
        logger.error(
            "Build system unavailable, cannot retrieve the build info of %s.",
            nvr,
        )
        return None
    try:
        bsrc = bsys.getBuild(nvr)
    except Exception:
        logger.exception(
            "An error occured while retrieving the build info for %s.", nvr
        )
        return None

    bi = dict()
    if "source" in bsrc:
        bi["scmurl"] = bsrc["source"]
        logger.debug("Retrieved SCMURL for %s: %s", nvr, bi["scmurl"])
    else:
        logger.error("Cannot find any SCMURL associated with %s.", nvr)
        return None

    try:
        minfo = bsrc["extra"]["typeinfo"]["module"]
        bi["name"] = minfo["name"]
        bi["stream"] = minfo["stream"]
        bi["modulemd"] = minfo["modulemd_str"]
        logger.debug(
            "Actual name:stream for %s is %s:%s", nvr, bi["name"], bi["stream"]
        )
    except Exception:
        bi["name"] = None
        bi["stream"] = None
        bi["modulemd"] = None
        logger.debug("No module info for %s.", nvr)

    return bi


def get_build(comp, ns="rpms"):
    """Get the latest build NVR for the specified component.  Searches the
    component namespace trigger tag to locate this.  Note this is not the
    highest NVR, it's the latest tagged build.

    :param comp: The component name
    :param ns: The component namespace
    :returns: NVR of the latest build, or None on error
    """
    if "main" not in c:
        logger.critical("DistroBaker is not configured, aborting.")
        return None
    bsys = get_buildsys("source")
    if bsys is None:
        logger.error(
            "Build system unavailable, cannot find the latest build for %s/%s.",
            ns,
            comp,
        )
        return None

    if ns == "rpms":
        try:
            nvr = bsys.listTagged(
                c["main"]["trigger"][ns], package=comp, latest=True
            )
        except Exception:
            logger.exception(
                "An error occured while getting the latest build for %s/%s.",
                ns,
                comp,
            )
            return None
        if nvr:
            logger.debug(
                "Located the latest build for %s/%s: %s",
                ns,
                comp,
                nvr[0]["nvr"],
            )
            return nvr[0]["nvr"]
        logger.error("Did not find any builds for %s/%s.", ns, comp)
        return None

    if ns == "modules":
        ms = split_module(comp)
        cname = ms["name"]
        sname = ms["stream"]
        try:
            builds = bsys.listTagged(
                c["main"]["trigger"][ns],
            )
        except Exception:
            logger.exception(
                "An error occured while getting the latest builds for %s/%s.",
                ns,
                cname,
            )
            return None
        if not builds:
            logger.error("Did not find any builds for %s/%s.", ns, cname)
            return None
        logger.debug(
            "Found %d total builds for %s/%s",
            len(builds),
            ns,
            cname,
        )
        # find the latest build for name:stream
        latest = None
        for b in builds:
            binfo = get_build_info(b["nvr"])
            if (
                binfo is None
                or binfo["name"] is None
                or binfo["stream"] is None
            ):
                logger.error(
                    "Could not get module info for %s, skipping.",
                    b["nvr"],
                )
            elif cname == binfo["name"] and sname == binfo["stream"]:
                latest = b["nvr"]
        if latest:
            logger.debug(
                "Located the latest build for %s/%s: %s", ns, comp, latest
            )
            return latest
        logger.error("Did not find any builds for %s/%s.", ns, comp)
        return None

    logger.error("Unrecognized namespace: %s/%s", ns, comp)
    return None


def get_buildsys(which):
    """Get a koji build system session for either the source or the
    destination.  Caches the sessions so future calls are cheap.
    Destination sessions are authenticated, source sessions are not.

    :param which: Session to select, source or destination
    :returns: Koji session object, or None on error
    """
    if "main" not in c:
        logger.critical("DistroBaker is not configured, aborting.")
        return None
    if which not in ("source", "destination"):
        logger.error('Cannot get "%s" build system.', which)
        return None

    session_timed_out = False
    if hasattr(get_buildsys, which):
        session_age = datetime.datetime.now() - getattr(
            get_buildsys, which + "_session_start_time"
        )
        # slightly less than an hour, to be safe
        if session_age.seconds > 3550 or session_age.days > 0:
            session_timed_out = True

    if session_timed_out or not hasattr(get_buildsys, which):
        logger.debug(
            'Initializing the %s koji instance with the "%s" profile.',
            which,
            c["main"][which]["profile"],
        )
        try:
            bsys = koji.read_config(profile_name=c["main"][which]["profile"])
            bsys = koji.ClientSession(bsys["server"], opts=bsys)
        except Exception:
            logger.exception(
                'Failed initializing the %s koji instance with the "%s" profile, skipping.',
                which,
                c["main"][which]["profile"],
            )
            return None
        logger.debug("The %s koji instance initialized.", which)
        if which == "destination":
            logger.debug("Authenticating with the destination koji instance.")
            try:
                if session_timed_out:
                    bsys.logout()
                bsys.gssapi_login()
            except Exception:
                logger.exception(
                    "Failed authenticating against the destination koji instance, skipping."
                )
                return None
            logger.debug(
                "Successfully authenticated with the destination koji instance."
            )
        if which == "source":
            get_buildsys.source = bsys
            get_buildsys.source_session_start_time = datetime.datetime.now()
        else:
            get_buildsys.destination = bsys
            get_buildsys.destination_session_start_time = (
                datetime.datetime.now()
            )
    else:
        logger.debug(
            "The %s koji instance is already initialized, fetching from cache.",
            which,
        )
    return vars(get_buildsys)[which]
