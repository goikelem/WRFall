#!/usr/bin/env python3
# =============================================================================
#  wrfall_install.py
#
#  A python installer / updater for WRF + WPS (ARW, Chem, Hydro, Fire) that needs
#  no root access.  It follows the structure of wrfall types:
#
#     initialize -> parse flags -> greet -> configure -> list packages ->
#     install | update | addpacks -> post-configure -> guidance
#
#  and wraps the logic of these scripts:
#     install_wrf_4.6.0.sh      (libraries, WRF/WPS build, env files, Do-List)
#     setup_wrf_domain.sh       (--mkdomain)
#     download_025_gfs.sh       (--getdata gfs)
#     download_ecmwf_ifs_025.sh (--getdata ecmwf)
#     download_era5.sh / .py    (--getdata era5)
#     run_wrf.sh                (--run)
#
#  Python 3.6+, standard library only (cdsapi is needed only for ERA5 data).
#
#  v1.4.2 - -chem: FLEX=<prefix>/bin/flex exported during the build and in the generated env files;
#           YACC/FLEX assignments in configure.wrf re-pointed to the local byacc/flex after
#           ./configure (they would otherwise override the exported env vars); functional
#           flex/yacc smoke test before compiling; warning about the WRF >= 4.7 KPP build bug
#           (wrf-model/wrf issue #2259); richer kpp_hint (kpp binary never built, broken apt
#           flex and segfault signatures).
#  v1.4.1 - -chem: absolute YACC path in build/env files, pre-build check for yacc/flex/libfl (KPP),
#           and a hint when the KPP build fails (Error 127 / 'third body ... M').
#  v1.4.0 - wrf_output/{metgrid,ungrib,Plots,geogrid}; one shared <home>/Scripts (utility scripts +
#           WRF-domain-Wizard); --getdata reads the d01 lat/lon extent from namelist.wps (+ --margin);
#           context-aware welcome / closing messages.
#  v1.3.0 - domains: GFS_Input/ECMWF_Input/ERA5_Input/YYYYMMDDCC/ sub-dirs, wrf_output/{metgrid,ungrib,geogrid},
#           <home>/Scripts (utility scripts + WRF-domain-Wizard), wrfall soft link, --domain a,b,c, --run auto gfs|ecmwf|era5
#  v1.2.0 - faster downloads: parallel HTTP range segments (--dl-jobs, default 8),
#           per-segment resume after Ctrl-C / connection drops, streamed GRIB
#           downloads (--getdata no longer loads whole files into RAM), live MB/s.
#
#  Quick start:
#     ./wrfall_install.py --install -arw
#     ./wrfall_install.py --install -fire            (WRF-Fire, real-data runs)
#     ./wrfall_install.py --install -fireideal       (WRF-Fire ideal cases, em_fire)
#     ./wrfall_install.py --install list
#     ./wrfall_install.py --update
#     ./wrfall_install.py --addpack domwiz,scripts --addgeog chem
#     ./wrfall_install.py --mkdomain ethiowrf
#     ./wrfall_install.py --mkdomain firedom -fire   (adds &fire, sr_x/sr_y, subgrid_ratio)
#     ./wrfall_install.py --addgeog fire             (landfire/ + highres_elev/ folders)
#     ./wrfall_install.py --getdata gfs --domain ~/Build_WRF/ethiowrf --date 20261016 --end 72
#     ./wrfall_install.py --run 20261016 00 72 --domain ~/Build_WRF/ethiowrf
# =============================================================================
import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

VERSION = "1.4.2"
EXE = os.path.basename(sys.argv[0]) or "wrfall_install.py"
UA = "Wget/1.21.4"   # some CDNs (e.g. Cloudflare in front of mpich.org) throttle unknown clients

DEFAULT_RELEASE = "4.6.0"
WRF_TAGS_API = "https://api.github.com/repos/wrf-model/WRF/releases?per_page=40"
WRF_URL = "https://github.com/wrf-model/WRF/releases/download/v{v}/v{v}.tar.gz"
WPS_URL = "https://github.com/wrf-model/WPS/archive/v{v}.tar.gz"
HYDRO_URL = "https://github.com/NCAR/wrf_hydro_nwm_public/archive/refs/tags/v5.3.0.tar.gz"
GEOG_BASE = "https://www2.mmm.ucar.edu/wrf/src/wps_files"
UTIL_RAW = "https://raw.githubusercontent.com/goikelem/Automation-Utility-Scripts/main/{f}"
DOMWIZ_REPO = "https://github.com/goikelem/WRF-domain-Wizard"

BUILD_TYPES = ["arw", "chem", "hydro", "fire", "fireideal"]
ALL_TYPES = ["arw", "chem", "hydro", "fire"]      # -all (fireideal is opt-in)
NO_WPS_TYPES = ("fireideal",)                      # types that build WRF only
TYPE_LABELS = {"arw": "ARW", "chem": "Chem", "hydro": "Hydro",
               "fire": "Fire", "fireideal": "FireIdeal"}

# ----------------------------------------------------------------------------
#  WRF-Fire (WRF Users Guide, chapter "WRF-Fire")
#    -fire       : standard WRF-ARW source, ./compile em_real, plus WPS prepared
#                  for the fire grid (NFUEL_CAT / ZSF with subgrid=yes).
#    -fireideal  : ./compile em_fire (hill_simple, two_fires), WRF only.
#  No special environment variable is needed; the fire module (ifire=2, level
#  set method) is part of every WRF-ARW build and is switched on in namelist.input.
# ----------------------------------------------------------------------------
FIRE_SR = 10                      # sr_x / sr_y / subgrid_ratio_x / subgrid_ratio_y (must be even)
FIRE_GEOG_DIRS = ("landfire", "highres_elev")
_SEP = "=" * 32
FIRE_GEOGRID_ENTRIES = (
    "name=NFUEL_CAT\n"
    "        priority=1\n"
    "        dest_type=categorical\n"
    "        dominant_only=NFUEL_CAT\n"
    "        z_dim_name=fuel_cat\n"
    "        halt_on_missing=yes\n"
    "        interp_option=default:nearest_neighbor+average_16pt+search\n"
    "        rel_path=default:landfire/\n"
    "        subgrid=yes\n" + _SEP + "\n"
    "name=ZSF\n"
    "        priority=1\n"
    "        dest_type=continuous\n"
    "        halt_on_missing=yes\n"
    "        interp_option=default:four_pt\n"
    "        rel_path=default:highres_elev/\n"
    "        subgrid=yes\n" + _SEP + "\n")
FIRE_NAMELIST_INPUT = """&fire
 ifire                  = 2,
 fire_fuel_read         = -1,
 fire_num_ignitions     = 1,
 fire_ignition_start_x1 = 1000.,
 fire_ignition_start_y1 = 1000.,
 fire_ignition_end_x1   = 1000.,
 fire_ignition_end_y1   = 1000.,
 fire_ignition_radius1  = 50.,
 fire_ignition_time1    = 600.,
 fire_print_msg         = 1,
 fire_print_file        = 0,
 fire_topo_from_atm     = 0,
/
"""

# ----------------------------------------------------------------------------
#  Libraries (links and build steps taken from install_wrf_4.6.0.sh)
# ----------------------------------------------------------------------------
LIBS = [
    dict(name="zlib", dir="zlib-1.2.13", tar="zlib-1.2.13.tar.gz",
         url="https://github.com/madler/zlib/releases/download/v1.2.13/zlib-1.2.13.tar.gz",
         conf=[]),
    dict(name="hdf5", dir="hdf5-1.12.2", tar="hdf5-1.12.2.tar.gz",
         url="https://support.hdfgroup.org/ftp/HDF5/releases/hdf5-1.12/hdf5-1.12.2/src/hdf5-1.12.2.tar.gz",
         conf=["--with-zlib={P}", "--enable-parallel=no", "--enable-hl", "--enable-fortran"]),
    dict(name="netcdf_c", dir="netcdf-c-4.9.2", tar="netcdf-c-4.9.2.tar.gz",
         url="https://github.com/Unidata/netcdf-c/archive/refs/tags/v4.9.2.tar.gz",
         conf=["--with-hdf5={P}", "--disable-dap", "--disable-byterange"]),
    dict(name="netcdf_fortran", dir="netcdf-fortran-4.6.1", tar="netcdf-fortran-4.6.1.tar.gz",
         url="https://github.com/Unidata/netcdf-fortran/archive/refs/tags/v4.6.1.tar.gz",
         conf=["--with-netcdf={P}"], env={"LIBS": "-lnetcdf -lhdf5_hl -lhdf5 -lz"}),
    dict(name="mpich", dir="mpich-4.1.2", tar="mpich-4.1.2.tar.gz",
         url="https://www.mpich.org/static/downloads/4.1.2/mpich-4.1.2.tar.gz", conf=[]),
    dict(name="libpng", dir="libpng-1.6.39", tar="libpng-1.6.39.tar.gz",
         url="https://sourceforge.net/projects/libpng/files/libpng16/1.6.39/libpng-1.6.39.tar.gz", conf=[]),
    dict(name="jasper", dir="jasper-1.900.1", tar="jasper-1.900.1.tar.gz",
         url="https://www2.mmm.ucar.edu/wrf/OnLineTutorial/compile_tutorial/tar_files/jasper-1.900.1.tar.gz",
         conf=[]),
    dict(name="flex", dir="flex-2.6.4", tar="flex-2.6.4.tar.gz",
         url="https://github.com/westes/flex/releases/download/v2.6.4/flex-2.6.4.tar.gz", conf=[]),
    dict(name="byacc", dir="byacc-*", tar="byacc.tar.gz",
         url="https://invisible-island.net/datafiles/release/byacc.tar.gz", conf=[]),
]

# ----------------------------------------------------------------------------
#  Terrestrial datasets and optional packages
# ----------------------------------------------------------------------------
GEOG_MANDATORY = ("geog_high_res_mandatory", "geog_high_res_mandatory.tar.gz")
CHEM_GEOGS = ["modis_landuse_21class_30s", "soiltype_top_2m", "soiltype_bot_2m",
              "albedo_ncep", "maxsnowalb", "erod", "clayfrac_5m", "sandfrac_5m"]
ADDGEOG_GROUPS = {"chem": CHEM_GEOGS, "mandatory": ["geog_high_res_mandatory"],
                  "fire": []}   # fire = local data folders only (no UCAR download)
UTIL_SCRIPTS = ["setup_wrf_domain.sh", "run_wrf.sh", "download_025_gfs.sh",
                "download_ecmwf_ifs_025.sh", "download_era5.sh", "download_era5.py"]
ADDPACKS = {
    "domwiz":  "WRF Domain Wizard (git clone of %s)" % DOMWIZ_REPO,
    "scripts": "Your original automation shell/python scripts, saved under <home>/Scripts",
    "cdsapi":  "The Python cdsapi module (pip --user) needed for ERA5 downloads",
}
ADDPACK_ALIASES = {"dwiz": "domwiz", "domainwizard": "domwiz", "wizard": "domwiz",
                   "utils": "scripts", "script": "scripts", "cds": "cdsapi", "era5": "cdsapi"}

# Rough planning figures (GB) shown to the user, not guarantees.
SPACE_GB = {"libs": 2, "wrf": 3, "wps": 1, "geog": 30, "chem_geog": 12}

S = {}  # global state, like %Install all version of wrf with options -chem, hydro, fire or arw


# =============================================================================
#  Messaging
# =============================================================================
def say(text="", sym="*", lead=1, trail=1, indent=4):
    pad = " " * indent
    head = (sym + "  ") if sym else ""
    sys.stdout.write("\n" * lead)
    lines = str(text).split("\n")
    out = []
    for i, ln in enumerate(lines):
        out.append((pad + head if i == 0 else pad + " " * len(head)) + ln)
    sys.stdout.write("\n".join(out))
    sys.stdout.write("\n" * trail)
    sys.stdout.flush()


def info(t, **k):
    say(t, "*", **k)


def warn(t, **k):
    say(t, "!", **k)


def ok(t, **k):
    say(t, "\u2713", **k)


def die(text="Something died in the pursuit of science - Again!", code=1):
    say(text, "\u2620", lead=2, trail=1)
    say("Fix the problem above and run me again.", "\u263a", lead=0, trail=2)
    sys.exit(code)


def on_sigint(signum, frame):
    say("Ouch! - Aren't we a bit sensitive?! Let's do this again sometime very soon!",
        "", lead=3, trail=2)
    sys.exit(2)


def ask(prompt, default="Yes"):
    """Ask a yes/no question; --allyes answers for you."""
    if S.get("allyes"):
        return True
    try:
        ans = input("    %s [%s]: " % (prompt, default)).strip()
    except EOFError:
        ans = ""
    ans = ans or default
    return bool(re.match(r"^(y|ok)", ans, re.I))


def ask_value(prompt, default):
    if S.get("allyes"):
        return default
    try:
        ans = input("    %s [%s]: " % (prompt, default)).strip()
    except EOFError:
        ans = ""
    return ans or default


# =============================================================================
#  Process helpers
# =============================================================================
def which(prog):
    return shutil.which(prog) or ""


def run(cmd, cwd=None, env=None, log=None, stdin=None, shell=False, check=True):
    """Run a command, log output to a file, return exit code (dry-run aware)."""
    shown = cmd if isinstance(cmd, str) else " ".join(cmd)
    if S.get("debug") or S.get("dryrun"):
        say("%s%s" % ("[dry-run] " if S.get("dryrun") else "", shown), "dbg", lead=0, trail=1, indent=6)
    if S.get("dryrun"):
        return 0
    e = os.environ.copy()
    if env:
        e.update(env)
    fh = open(log, "a") if log else None
    try:
        p = subprocess.run(cmd, cwd=cwd, env=e, shell=shell,
                           input=stdin.encode() if stdin else None,
                           stdout=fh if fh else subprocess.PIPE,
                           stderr=subprocess.STDOUT)
    finally:
        if fh:
            fh.close()
    if check and p.returncode != 0:
        tail = ""
        if log and os.path.exists(log):
            with open(log, errors="replace") as f:
                tail = "".join(f.readlines()[-25:])
        elif p.stdout:
            tail = p.stdout.decode(errors="replace")[-2000:]
        die("Command failed (%d): %s\n\n%s" % (p.returncode, shown, tail))
    return p.returncode


def rm(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


# =============================================================================
#  System information (CollectSystemInfo)
# =============================================================================
def collect_system_info():
    i = {"host": socket.gethostname(), "kernel": os.uname().release, "arch": os.uname().machine}
    try:
        i["addr"] = socket.gethostbyname(i["host"])
    except OSError:
        i["addr"] = "127.0.0.1"
    distro = "Unknown"
    try:
        with open("/etc/os-release") as f:
            m = re.search(r'PRETTY_NAME="?([^"\n]+)', f.read())
            if m:
                distro = m.group(1)
    except OSError:
        pass
    i["distro"] = distro

    sockets, cores, threads, model = 1, 0, 1, "Unknown"
    try:
        out = subprocess.run(["lscpu"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env=dict(os.environ, LC_ALL="C")).stdout.decode()
        g = lambda k: (re.search(r"^%s:\s*(.+)$" % k, out, re.M) or [None, ""])[1].strip()
        sockets = int(g(r"Socket\(s\)") or 1)
        cores = int(g(r"Core\(s\) per socket") or 0)
        threads = int(g(r"Thread\(s\) per core") or 1)
        model = g("Model name") or model
    except (OSError, ValueError):
        pass
    total_logical = os.cpu_count() or 1
    if not cores:
        cores = max(1, total_logical // max(1, sockets * threads))
    i.update(sockets=sockets, cores_per_socket=cores, threads_per_core=threads,
             total_cores=sockets * cores, model=model, ht=threads > 1 and "amd" not in model.lower())
    mem = "Unknown"
    try:
        with open("/proc/meminfo") as f:
            kb = int(re.search(r"MemTotal:\s+(\d+)", f.read()).group(1))
            mem = "%.2f" % (kb / 1048576.0)
    except (OSError, AttributeError):
        pass
    i["mem_gb"] = mem
    return i


def format_system_info(i):
    return ("System information for %(host)s:\n\n"
            "  Machine Address     : %(addr)s\n"
            "  Linux Kernel        : %(kernel)s\n"
            "  Linux Distribution  : %(distro)s\n\n"
            "  CPU Name            : %(model)s\n"
            "  CPU Type            : %(arch)s\n"
            "  Sockets             : %(sockets)s\n"
            "  Cores per Socket    : %(cores_per_socket)s\n"
            "  Total Physical CPUs : %(total_cores)s\n"
            "  Available Memory    : %(mem_gb)s GB" % i)


# =============================================================================
#  Release / state handling
# =============================================================================
def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def release_file(home):
    return os.path.join(home, "strc", ".release")


def read_release(home):
    """Return dict(wrfems=..., types=[...]) for an existing installation, or {}."""
    f = release_file(home)
    if not os.path.isfile(f):
        return {}
    d = {}
    for ln in open(f):
        k, _, v = ln.strip().partition(" ")
        d[k.lower()] = v.strip()
    d["types"] = [t for t in d.get("types", "").split(",") if t]
    return d


def write_release(home, release, types):
    os.makedirs(os.path.join(home, "strc"), exist_ok=True)
    old = read_release(home)
    merged = sorted(set(old.get("types", [])) | set(types))
    with open(release_file(home), "w") as f:
        f.write("WRFEMS %s\nWRF %s\nWPS %s\nTYPES %s\nDATE %s\n" % (
            release, release, release, ",".join(merged),
            dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")))


def available_releases():
    """Query GitHub for WRF release tags; fall back to the built-in default."""
    rels = []
    try:
        req = urllib.request.Request(WRF_TAGS_API, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            for item in json.loads(r.read().decode()):
                m = re.match(r"^v(\d+\.\d+(?:\.\d+)?)$", item.get("tag_name", ""))
                if m:
                    rels.append(m.group(1))
    except Exception:
        pass
    if DEFAULT_RELEASE not in rels:
        rels.append(DEFAULT_RELEASE)
    return sorted(set(rels), key=vtuple, reverse=True)


# =============================================================================
#  Download / package handling
# =============================================================================
def http_size(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return int(r.headers.get("Content-Length", 0))
    except Exception:
        return 0


# --- parallel segmented downloader -------------------------------------------
#  Large tarballs (WRF/WPS, WPS_GEOG, Chem datasets, ...) are fetched with
#  several concurrent HTTP range requests instead of one stream.  All hosts
#  used here (GitHub/S3, UCAR, Cloudflare-fronted mirrors) allow ranges, and
#  this typically multiplies throughput on high-latency or per-connection
#  throttled links.  Every segment keeps a <file>.pNNN marker with its
#  progress, so an interrupted transfer (Ctrl-C, drop, timeout) resumes
#  where it stopped.  --dl-jobs 1 restores plain single-stream mode.
DL_CHUNK = 1 << 20                        # read/write chunk size (1 MiB)
DL_MIN_PARALLEL = 8 * (1 << 20)           # parallel segments only for files >= 8 MiB
DL_MAX_JOBS = 16                          # hard cap for --dl-jobs


def _probe(url):
    """Return (size_in_bytes, ranges_supported); (0, False) when unknown."""
    hdr = {"User-Agent": UA}
    try:
        req = urllib.request.Request(url, headers=hdr, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            cl = int(r.headers.get("Content-Length") or 0)
            if cl:
                return cl, (r.headers.get("Accept-Ranges") or "").lower() == "bytes"
    except Exception:
        pass
    try:                                   # some hosts dislike HEAD: 1-byte ranged GET
        h = dict(hdr)
        h["Range"] = "bytes=0-0"
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
            m = re.match(r"bytes\s+\d+-\d+/(\d+)", r.headers.get("Content-Range") or "")
            if r.status == 206 and m:
                return int(m.group(1)), True
            if r.status == 200:
                return int(r.headers.get("Content-Length") or 0), False
    except Exception:
        pass
    return 0, False


def _seg_files(part):
    return glob.glob(part + ".p[0-9][0-9][0-9]")


def _dl_cleanup(part):
    rm(part)
    for f in _seg_files(part):
        rm(f)


def _human(n):
    return "%.1f MB" % (n / 1048576.0) if n < (1 << 30) else "%.2f GB" % (n / 1073741824.0)


def _fmt_rate(bps):
    return "%5.1f MB/s" % (bps / 1048576.0) if bps >= 1048576 else "%5.0f KB/s" % (bps / 1024.0)


def _progress_line(done, total, rate):
    pct = 100.0 * done / total if total else 0.0
    eta = int((total - done) / rate) if rate > 1 and total > done else 0
    sys.stdout.write("\r        %5.1f%% of %-9s %s  ETA %02d:%02d:%02d   "
                     % (pct, _human(total), _fmt_rate(rate), eta // 3600, eta % 3600 // 60, eta % 60))
    sys.stdout.flush()


def _save_pos(pfh, start, end, pos):
    pfh.seek(0)
    pfh.truncate()
    pfh.write("%d %d %d" % (start, end, pos))   # bounds stored so a changed
    pfh.flush()                                 # --dl-jobs never mis-resumes


def _dl_worker(url, part, segno, start, end, state, lock):
    """Download bytes [start, end] of url into part at the correct offsets."""
    posf = part + ".p%03d" % segno
    seg_len = end - start + 1
    pos = 0
    if os.path.isfile(posf):
        try:
            m0, m1, m2 = open(posf).read().split()
            if int(m0) == start and int(m1) == end:     # same segment layout only
                pos = max(0, min(int(m2), seg_len))
        except (ValueError, OSError):
            pos = 0
    with lock:
        state["done"] += pos
    pfh = open(posf, "w")
    try:
        for attempt in range(1, 6):
            if state["abort"].is_set() or pos >= seg_len:
                return
            try:
                hdr = {"User-Agent": UA, "Range": "bytes=%d-%d" % (start + pos, end)}
                with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=90) as r:
                    if r.status == 416:                    # segment already complete
                        _save_pos(pfh, start, end, seg_len)
                        return
                    if r.status != 206:
                        state["err"] = state["err"] or ("server ignored Range (HTTP %d)" % r.status)
                        state["abort"].set()
                        return
                    with open(part, "r+b") as f:
                        f.seek(start + pos)
                        while True:
                            chunk = r.read(DL_CHUNK)
                            if not chunk:
                                break
                            f.write(chunk)
                            f.flush()
                            pos += len(chunk)
                            _save_pos(pfh, start, end, pos)
                            with lock:
                                state["done"] += len(chunk)
                            if state["abort"].is_set():
                                return
                if pos >= seg_len:
                    return
                state["err"] = state["err"] or ("connection closed early (%d of %d segment bytes)" % (pos, seg_len))
            except urllib.error.HTTPError as e:
                if e.code == 416:
                    _save_pos(pfh, start, end, seg_len)
                    return
                state["err"] = state["err"] or ("HTTP %d" % e.code)
                if e.code in (401, 403, 404):
                    state["abort"].set()
                    return
            except Exception as e:
                state["err"] = state["err"] or str(e)
            time.sleep(min(2 * attempt, 8))
        state["err"] = state["err"] or "a segment ran out of retries"
    finally:
        pfh.close()


def _download_parallel(url, part, total, jobs):
    """Fetch url into part via jobs concurrent range requests.

    Returns (0, "") on success, (1, err) on a retryable failure and
    (2, err) when the server does not really honour range requests.
    """
    jobs = max(2, min(int(jobs), DL_MAX_JOBS))
    lock = threading.Lock()
    state = {"done": 0, "err": "", "abort": threading.Event()}
    if not _seg_files(part) or not os.path.isfile(part) or os.path.getsize(part) != total:
        _dl_cleanup(part)
        with open(part, "wb") as f:            # preallocate (sparse)
            f.truncate(total)
    bounds, start = [], 0
    seg = total // jobs
    for i in range(jobs):
        end = start + seg - 1 if i < jobs - 1 else total - 1
        if end >= start:
            bounds.append((i, start, end))
        start = end + 1
    threads = [threading.Thread(target=_dl_worker,
                                args=(url, part, i, s0, e0, state, lock), daemon=True)
               for (i, s0, e0) in bounds]
    for t in threads:
        t.start()
    t0, last, last_t = time.time(), 0, time.time()
    while any(t.is_alive() for t in threads):
        time.sleep(0.3)
        now = time.time()
        if sys.stdout.isatty() and now - last_t >= 0.4:
            with lock:
                done = state["done"]
            if done > last:
                _progress_line(done, total, (done - last) / (now - last_t))
                last, last_t = done, now
    for t in threads:
        t.join()
    if state["err"] and "ignored Range" in state["err"]:
        return 2, state["err"]
    if state["err"]:
        return 1, state["err"]
    for (i, s0, e0) in bounds:                 # every segment must be complete
        try:
            b0, b1, b2 = open(part + ".p%03d" % i).read().split()
            if (int(b0), int(b1), int(b2)) != (s0, e0, e0 - s0 + 1):
                return 1, "segment %d incomplete" % i
        except (ValueError, OSError):
            return 1, "segment %d marker missing" % i
    if os.path.getsize(part) != total:
        return 1, "size mismatch after download (%d != %d)" % (os.path.getsize(part), total)
    for f in _seg_files(part):
        rm(f)
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 76 + "\r")
        sys.stdout.flush()
    return 0, ""


def _download_single(url, part):
    """Plain single-stream download with resume and progress (the old behaviour)."""
    err = ""
    for attempt in range(1, 4):
        try:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            hdr = {"User-Agent": UA}
            if have:
                hdr["Range"] = "bytes=%d-" % have
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=90) as r:
                mode = "ab" if (have and r.status == 206) else "wb"
                total = int(r.headers.get("Content-Length", 0)) + (have if mode == "ab" else 0)
                done = have if mode == "ab" else 0
                t0, last, last_t = done, done, time.time()
                with open(part, mode) as f:
                    while True:
                        chunk = r.read(DL_CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if sys.stdout.isatty() and total:
                            now = time.time()
                            if now - last_t >= 0.4:
                                _progress_line(done, total, (done - last) / (now - last_t))
                                last, last_t = done, now
                if sys.stdout.isatty():
                    sys.stdout.write("\r" + " " * 76 + "\r")
                    sys.stdout.flush()
                return 0, ""
        except urllib.error.HTTPError as e:
            if e.code == 416:      # already complete
                return 0, ""
            err = "HTTP %d" % e.code
        except Exception as e:
            err = str(e)
        time.sleep(3 * attempt)
    return 1, err


def _download_urllib(url, part, jobs=None):
    """Download url -> part: parallel range segments when possible, resumable."""
    if jobs is None:
        jobs = S.get("dl_jobs", 8)
    try:
        njobs = max(1, min(int(jobs), DL_MAX_JOBS))
    except (TypeError, ValueError):
        njobs = 8
    err = ""
    for attempt in range(1, 4):
        size, ranges = _probe(url)
        if njobs > 1 and ranges and size >= DL_MIN_PARALLEL:
            rc, err = _download_parallel(url, part, size, njobs)
            if rc == 0:
                return 0
            if rc == 2:                        # ranges not honoured after all
                _dl_cleanup(part)
                rc, err = _download_single(url, part)
                if rc == 0:
                    return 0
        else:
            if _seg_files(part):               # leftovers from an older parallel run
                _dl_cleanup(part)
            rc, err = _download_single(url, part)
            if rc == 0:
                return 0
        time.sleep(3 * attempt)
    say("Download failed: %s (%s)" % (url, err), "!", lead=0)
    return 1


def acquire(url, dest):
    """Copy from the repository or download via urllib/curl/wget to dest."""
    base = os.path.basename(dest)
    if S.get("repodir"):
        for cand in glob.glob(os.path.join(S["repodir"], "*", base)) + [os.path.join(S["repodir"], base)]:
            if os.path.isfile(cand):
                if not S["dryrun"]:
                    shutil.copy2(cand, dest)
                return "Copied"
        die("File %s not found in repository %s" % (base, S["repodir"]))
    if S["dryrun"]:
        say("would download %s -> %s" % (url, dest), "dbg", lead=0, indent=6)
        return "Downloaded"
    part = dest + ".part"
    m = S["method"]
    # "auto": the system downloaders were measured much faster than the pure-Python
    # fetcher, so try wget, then curl, and use urllib only as the last resort.
    order = [m] if m != "auto" else [x for x in ("wget", "curl") if which(x)] + ["urllib"]
    rc = 1
    for meth in order:
        if meth == "wget":
            rc = subprocess.call(["wget", "-c", "-q", "--show-progress", "--progress=bar:force", "-t", "5",
                                  "--connect-timeout=25", "-O", part, url])
        elif meth == "curl":
            rc = subprocess.call(["curl", "-fL", "-C", "-", "--retry", "3", "--connect-timeout", "25",
                                  "-o", part, url])
        else:
            rc = _download_urllib(url, part)
        if rc == 0 and os.path.isfile(part) and os.path.getsize(part) > 0:
            break
        if len(order) > 1:
            warn("%s failed for %s - trying the next method" % (meth, os.path.basename(dest)), lead=0)
    if rc != 0 or not os.path.isfile(part) or os.path.getsize(part) == 0:
        rm(part)
        die("Failed during acquisition of %s - check connection & local file system" % url)
    os.replace(part, dest)
    return "Downloaded"


def package(url, filename, label=None):
    """Make sure a package file exists in <home>/packages (Build_WRF: package tarfiles)."""
    dest = os.path.join(S["pkgdir"], filename)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0 and not S["nolocal"]:
        sys.stdout.write("        %-44s - Found locally\n" % filename)
        return dest
    how = acquire(url, dest)
    sys.stdout.write("        %-44s - %s\n" % (filename, how))
    return dest


def unpack(tarfile_path, dest_dir):
    if S["nounpack"]:
        return
    os.makedirs(dest_dir, exist_ok=True)
    run(["tar", "-xf", tarfile_path, "-C", dest_dir])


def unpack_single(tarfile_path, final_dir):
    """Unpack an archive containing one top-level directory and rename it to final_dir."""
    if S["dryrun"]:
        run(["tar", "-xf", tarfile_path, "-C", S["home"]])
        return
    tmp = tempfile.mkdtemp(prefix=".unpack_", dir=S["home"])
    try:
        run(["tar", "-xf", tarfile_path, "-C", tmp])
        entries = os.listdir(tmp)
        src = os.path.join(tmp, entries[0]) if len(entries) == 1 else tmp
        rm(final_dir)
        shutil.move(src, final_dir)
    finally:
        rm(tmp)


# =============================================================================
#  Paths and environment
# =============================================================================
def set_paths(home):
    S["home"] = os.path.abspath(os.path.expanduser(home))
    S["libdir"] = os.path.join(S["home"], "LIBRARIES")
    S["prefix"] = os.path.join(S["home"], "local")
    S["pkgdir"] = os.path.join(S["home"], "packages")
    S["moddir"] = os.path.join(S["home"], "etc", "modulefiles")
    S["logdir"] = os.path.join(S["home"], "logs")
    S["geogdir"] = os.path.join(S["home"], "WPS_GEOG")


def make_dirs():
    if S["dryrun"]:
        return
    for d in (S["home"], S["libdir"], S["pkgdir"], S["moddir"], S["logdir"],
              os.path.join(S["prefix"], "include"), os.path.join(S["prefix"], "lib"),
              os.path.join(S["prefix"], "bin")):
        os.makedirs(d, exist_ok=True)


def wrf_dir(typ, ver):
    return os.path.join(S["home"], "WRF-%s-%s" % (ver, type_label(typ)))


def wps_dir(typ, ver):
    return os.path.join(S["home"], "WPS-%s-%s" % (ver, type_label(typ)))


def type_label(typ):
    return TYPE_LABELS.get(typ, typ.capitalize())


MODERN_GCC_FLAGS = "-m64 -fallow-argument-mismatch"


def gfortran_major():
    """Major version of gfortran, or 99 if it cannot be determined."""
    try:
        return int(subprocess.run(["gfortran", "-dumpversion"], stdout=subprocess.PIPE)
                   .stdout.decode().split(".")[0])
    except (OSError, ValueError):
        return 99


def chem_needs_modern_gcc(typ, ver):
    """WRF-Chem >= 4.7 (4.7.x, 4.8.x ...) passes arguments of mixed type/rank to the same routine in
    the chem/ sources; gfortran >= 10 turns that into a hard error unless -fallow-argument-mismatch is
    given.  WRF 4.6.x does not need it, so its (working) build is left untouched."""
    return typ == "chem" and ver is not None and vtuple(ver) >= (4, 7) and gfortran_major() >= 10


def build_env(typ=None, ver=None):
    p = S["prefix"]
    e = dict(CC="gcc", CXX="g++", FC="gfortran", F77="gfortran",
             CFLAGS="-O3 -fPIC", FCFLAGS="-O3 -fPIC", FFLAGS="-O3 -fPIC",
             LDFLAGS="-L%s/lib" % p, CPPFLAGS="-I%s/include" % p,
             DIR=S["home"], PREFIX=p, NETCDF=p, HDF5=p,
             JASPERLIB=p + "/lib", JASPERINC=p + "/include",
             PATH=p + "/bin:" + os.environ.get("PATH", ""),
             LD_LIBRARY_PATH=p + "/lib" + (":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else ""),
             LC_ALL="C")
    if typ == "chem":
        e.update(WRF_CHEM="1", WRF_KPP="1", FLEX_LIB_DIR=p + "/lib",
                 FLEX=p + "/bin/flex", YACC=p + "/bin/yacc -d")
        if chem_needs_modern_gcc(typ, ver):
            e.update(FCFLAGS="-O3 -fPIC " + MODERN_GCC_FLAGS, FFLAGS="-O3 -fPIC " + MODERN_GCC_FLAGS)
    elif typ == "hydro":
        e.update(WRF_HYDRO="1")
    return e


def clean_environ_for_build():
    for k in ("WRF_CHEM", "WRF_KPP", "WRF_HYDRO", "FLEX_LIB_DIR", "FLEX", "YACC"):
        os.environ.pop(k, None)


# =============================================================================
#  Prerequisites
# =============================================================================
def check_prereqs():
    need = ["gcc", "g++", "gfortran", "make", "tar", "perl"]
    miss = [p for p in need if not which(p)]
    if miss and S.get("dryrun"):
        warn("Missing build tools (ignored in --dryrun): %s" % ", ".join(miss))
    elif miss:
        die("Missing required build tools: %s\n\n"
            "Install them with your package manager, for example:\n"
            "    Debian/Ubuntu : sudo apt install build-essential gfortran perl tcsh m4 git\n"
            "    RHEL/Rocky    : sudo dnf groupinstall 'Development Tools' && sudo dnf install gcc-gfortran perl tcsh m4 git"
            % ", ".join(miss))
    soft = []
    if not (which("tcsh") or which("csh")):
        soft.append("tcsh/csh (WPS compile and link_grib.csh need it)")
    if not which("m4"):
        soft.append("m4 (needed by some library builds)")
    if S["method"] in ("curl", "wget") and not which(S["method"]):
        die("You requested --%s but it is not installed." % S["method"])
    if soft:
        warn("Optional tools not found: " + "; ".join(soft))


def disk_estimate_gb(types, nogeog, chem):
    t = SPACE_GB["libs"] + len(types) * (SPACE_GB["wrf"] + SPACE_GB["wps"])
    if not nogeog:
        t += SPACE_GB["geog"]
    if chem and not nogeog:
        t += SPACE_GB["chem_geog"]
    return t


# =============================================================================
#  Library builds
# =============================================================================
def marker(name):
    return os.path.join(S["prefix"], ".installed_%s" % name)


# files that prove a support library is really installed under <prefix>
LIB_PROBES = {
    "zlib":           ["lib/libz.*", "include/zlib.h"],
    "hdf5":           ["lib/libhdf5.*", "lib/libhdf5_hl.*", "include/hdf5.h"],
    "netcdf_c":       ["lib/libnetcdf.*", "bin/nc-config"],
    "netcdf_fortran": ["lib/libnetcdff.*", "bin/nf-config"],
    "mpich":          ["bin/mpif90", "bin/mpiexec", "lib/libmpi*"],
    "libpng":         ["lib/libpng*", "include/png.h"],
    "jasper":         ["lib/libjasper.*", "include/jasper/jasper.h"],
    "flex":           ["bin/flex", "lib/libfl.*"],
    "byacc":          ["bin/yacc"],
}


def lib_present(name):
    """True when every probe file of the library exists under <prefix> (marker file not needed)."""
    pats = LIB_PROBES.get(name)
    if not pats:
        return os.path.isfile(marker(name))
    return all(glob.glob(os.path.join(S["prefix"], pat)) for pat in pats)


def lib_done(name):
    """Skip a library that is already installed.  --force rebuilds WRF/WPS only; use
    --force-libs to rebuild the support libraries as well."""
    if S.get("force_libs") or S.get("dryrun"):
        return False
    return lib_present(name)


def build_libraries():
    info("Building %d support libraries into %s" % (len(LIBS), S["prefix"]), lead=2)
    n = len(LIBS)
    for idx, lib in enumerate(LIBS, 1):
        name = lib["name"]
        if lib_done(name):
            say("[%d/%d] %s already installed (skipping)" % (idx, n, name), "\u2713", lead=0, indent=6)
            if not os.path.isfile(marker(name)) and not S["dryrun"]:
                open(marker(name), "w").close()
            continue
        say("[%d/%d] %s" % (idx, n, name), "->", lead=0, indent=6)
        tarp = package(lib["url"], lib["tar"])
        env = build_env()
        env.update(lib.get("env", {}))
        log = os.path.join(S["logdir"], "lib_%s.log" % name)
        if not S["dryrun"]:
            rm(log)
        # unpack
        pattern = os.path.join(S["libdir"], lib["dir"])
        have = glob.glob(pattern)
        if not have or S["force"] or not S["continue"]:
            for h in have:
                rm(h)
            unpack(tarp, S["libdir"])
            have = glob.glob(pattern)
        if not have and not S["dryrun"]:
            die("Unpacking %s did not create %s" % (lib["tar"], lib["dir"]))
        src = have[0] if have else pattern
        conf = [c.replace("{P}", S["prefix"]) for c in lib["conf"]]
        run(["./configure", "--prefix=" + S["prefix"]] + conf, cwd=src, env=env, log=log)
        run(["make", "-j%d" % S["makej"]], cwd=src, env=env, log=log)
        run(["make", "install"], cwd=src, env=env, log=log)
        if not S["dryrun"]:
            open(marker(name), "w").close()
    ok("All libraries installed to %s (%s)" % (S["prefix"], "rebuilt" if S.get("force_libs") else "existing ones reused"), lead=1)


# =============================================================================
#  WRF / WPS builds
# =============================================================================
def check_kpp_tools():
    """WRF-Chem (-chem) builds KPP, which needs yacc + flex + libfl from <prefix>. Stop early if absent."""
    if S["dryrun"]:
        return
    p = S["prefix"]
    miss = [x for x in ("yacc", "flex") if not os.path.isfile(os.path.join(p, "bin", x))]
    libs = glob.glob(os.path.join(p, "lib*", "libfl.*"))
    if not libs:
        miss.append("libfl (flex library)")
    if miss:
        die("WRF-Chem needs yacc, flex and libfl for KPP, but %s not found under %s.\n"
            "Rebuild them from source and retry:\n"
            "    rm -f %s/.installed_flex %s/.installed_byacc\n"
            "    %s --install -chem --continue --force\n"
            "KPP needs flex built from SOURCE: the Debian/Ubuntu apt flex package is broken and\n"
            "makes every mechanism fail ('third body ... M', Error 127). Distro packages are only\n"
            "a fallback (sudo apt install byacc flex libfl-dev / dnf install byacc flex flex-devel)."
            % (", ".join(miss), p, p, p, EXE))
    # Functional smoke test: a broken flex (notably the Debian/Ubuntu apt package) or a parser
    # generated by bison 3.x makes every KPP mechanism fail later with Error 127 /
    # 'third body ... M' / a segfault.  Catch that here instead of after 40 minutes of compiling.
    try:
        out = subprocess.run([os.path.join(p, "bin", "flex"), "--version"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=30).stdout.decode(errors="replace").strip()
    except Exception as e:
        out = "flex --version failed: %s" % e
    if "flex" not in out.lower():
        die("The flex binary at %s/bin/flex is broken (\"%s\").\n"
            "A broken flex breaks every WRF-Chem KPP mechanism ('third body ... M', Error 127).\n"
            "Rebuild flex 2.6.4 from source and retry:\n"
            "    rm -f %s/.installed_flex\n"
            "    %s --install -chem --continue --force" % (p, out, p, EXE))
    say("flex OK: %s" % out, "\u2713", lead=0, indent=6)
    try:
        out = subprocess.run([os.path.join(p, "bin", "yacc"), "-V"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=30).stdout.decode(errors="replace").strip()
        say("yacc OK: %s" % (out or "byacc"), "\u2713", lead=0, indent=6)
    except Exception:
        warn("could not run %s/bin/yacc -V (continuing anyway)" % p, lead=0, indent=6)


ERR_RE = re.compile(r"(?i)(\berror\b[: ]|\*\*\* \[|undefined reference|multiple definition|"
                    r"segmentation fault|command not found|no such file|cannot find|fatal|"
                    r"unexpected case|third body|core dumped)")


def error_excerpt(log, n=14):
    """First n real error lines of a build log, with line numbers (the first error is the cause;
    everything after it is usually fallout)."""
    try:
        lines = open(log, errors="replace").read().splitlines()
    except OSError:
        return
    hits = [(i + 1, l.strip()) for i, l in enumerate(lines)
            if ERR_RE.search(l) and "Error: 0" not in l and not l.lstrip().startswith("#")]
    if not hits:
        return
    body = "\n".join("   %6d: %s" % (i, l[:170]) for i, l in hits[:n])
    warn("First errors in %s (the FIRST one is the cause, the rest is fallout):\n%s\n"
         "   Context:  sed -n '%d,%dp' %s" % (log, body, max(1, hits[0][0] - 12), hits[0][0] + 8, log), lead=1)


def kpp_hint(log, work=None):
    """After a failed chem compile: explain the usual KPP failure signatures."""
    error_excerpt(log)
    try:
        txt = open(log, errors="replace").read()
    except OSError:
        return
    wkc_sig = (re.search(r"undefined reference to `\S*jacobiansp\S*_lu_(diag|crow|icol)", txt)
               or "Unexpected CASE statement" in txt or "case-selector exists" in txt)
    if wkc_sig:
        warn("This is the WRF-Chem 4.7.x 'tuv_kpp FIRST' path bug in chem/KPP/compile_wkc, not a flex/yacc problem:\n"
             "the photolysis include files are never generated, which explains the KPP segfault, the\n"
             "'undefined reference ... jacobiansp ... lu_diag', Error 127 and module_phot_tuv's\n"
             "'Unexpected CASE statement'.  The installer now patches compile_wkc automatically\n"
             "(path -> ../../../../inc/); rebuild with:\n"
             "    %s --install %s -chem --force\n"
             "By hand: edit chem/KPP/compile_wkc so the line reads\n"
             "    $WKC_HOME/util/wkc/tuv_kpp FIRST ../../../../inc/\n"
             "and rebuild from a freshly unpacked tree (the installer does this with --force)."
             % (EXE, S.get("ver_hint") or "<version>"), lead=1)
        return
    kpp_bins = glob.glob(os.path.join(work or "", "chem", "KPP", "kpp", "kpp-*", "bin", "kpp")) if work else []
    if work and not kpp_bins:
        warn("The KPP program (chem/KPP/kpp/kpp-2.1/bin/kpp) was not built - the kpp build itself failed.\n"
             "Fix the flex/yacc/libfl setup and rebuild:\n"
             "    rm -f %s/.installed_flex %s/.installed_byacc\n"
             "    %s --install -chem --force-libs --force" % (S["prefix"], S["prefix"], EXE), lead=1)
    if "third body in KPP" in txt or "Error 127" in txt:
        warn("KPP failed (Error 127 / 'third body ... M'). Rebuild with the local tools - in the SAME shell:\n"
             "  conda deactivate 2>/dev/null           # conda's flex/bison shadow the local ones\n"
             "  export WRF_CHEM=1 WRF_KPP=1 PATH=%s/bin:$PATH\n"
             "  export YACC=\"%s/bin/yacc -d\" FLEX=\"%s/bin/flex\" FLEX_LIB_DIR=%s/lib\n"
             "KPP needs byacc (not bison 3.x) and flex built from source - the Debian/Ubuntu apt flex\n"
             "breaks every mechanism with exactly this error. WRF >= 4.7 also has a known KPP build bug\n"
             "(wrf-model/wrf issue #2259); if it still fails, use WRF 4.6.x:\n"
             "  %s --install 4.6.0 -chem --force"
             % (S["prefix"], S["prefix"], S["prefix"], S["prefix"], EXE), lead=1)
    if re.search(r"(Type|Rank) mismatch|fallow-argument-mismatch|Modern GCC compat", txt):
        warn("gfortran rejected mixed-type/rank arguments in the WRF-Chem sources (GCC >= 10).\n"
             "This installer adds the flags for WRF-Chem >= 4.7 automatically; if you compile by hand, use:\n"
             "    export FCFLAGS=\"-m64 -fallow-argument-mismatch\"\n"
             "    export FFLAGS=\"-m64 -fallow-argument-mismatch\"\n"
             "    (then re-run ./configure and add the flag to FCBASEOPTS_NO_G in configure.wrf)\n"
             "or rebuild:  %s --install %s -chem --force" % (EXE, "<version>"), lead=1)
    if "Segmentation fault" in txt:
        warn("A KPP-stage command segfaulted - the typical broken-flex (apt) signature. Rebuild it:\n"
             "    %s --install -chem --force-libs --force" % EXE, lead=1)
    if "Unexpected CASE statement" in txt:
        warn("module_phot_tuv failed with 'Unexpected CASE statement': check\n"
             "  grep -n -B3 -A3 'Unexpected CASE' %s" % log, lead=1)


def force_kpp_tool_paths(work):
    """./configure records the flex/yacc it found in configure.wrf, and those Makefile
    assignments override the exported FLEX/YACC environment variables.  Re-point them at
    the local byacc/flex so the whole KPP stage uses the right tools (system bison or the
    Debian/Ubuntu apt flex produce 'third body ... M', per-mechanism Error 127, segfaults)."""
    if S["dryrun"]:
        return
    p = S["prefix"]
    cw = os.path.join(work, "configure.wrf")
    if not os.path.isfile(cw):
        return
    txt = open(cw).read()
    new, n = re.subn(r"(?m)^(\s*YACC\s*=\s*).*$", r"\g<1>" + p + "/bin/yacc -d", txt)
    new, k = re.subn(r"(?m)^(\s*FLEX\s*=\s*).*$", r"\g<1>" + p + "/bin/flex", new)
    n += k
    if n:
        open(cw, "w").write(new)
        say("configure.wrf: %d YACC/FLEX assignment(s) re-pointed to %s/bin" % (n, p),
            "\u2713", lead=0, indent=6)
    else:
        warn("no YACC/FLEX assignments found in %s - KPP tools will be taken from PATH\n"
             "(%s/bin is first there, so byacc/flex from this installer win)" % (cw, p), lead=0, indent=6)


def fix_compile_wkc(work):
    """WRF-Chem 4.7.0/4.7.1 (old make-based build) bug in chem/KPP/compile_wkc: the first
    'tuv_kpp FIRST <dir>' call gets a too-short relative path to WRF's inc/ directory, so the
    photolysis include files are never written.  Consequences, all seen in the log:
      * 'Segmentation fault' from the KPP/WKC Makefile step,
      * undefined reference to '<mech>_jacobiansp_MOD_lu_diag/lu_crow/lu_icol' (write_decomp),
      * 'Error 127' (write_decomp.exe was never produced),
      * module_phot_tuv.f90: 'Unexpected CASE statement'.
    The WRF developers' fix (UCAR forum, threads 22663 and 22757): the path must be ../../../../inc/.
    Idempotent: a tree that already has the right path (e.g. a later release) is left alone."""
    if S["dryrun"]:
        return
    f = os.path.join(work, "chem", "KPP", "compile_wkc")
    if not os.path.isfile(f):
        warn("%s not found - cannot apply the tuv_kpp path fix" % f, lead=0, indent=6)
        return
    txt = open(f, errors="replace").read()
    want = "../../../../inc/"
    pat = re.compile(r"(tuv_kpp\s+FIRST\s+)((?:\.\./)+inc/)")
    m = pat.search(txt)
    if not m:
        warn("no 'tuv_kpp FIRST ...inc/' line found in %s - nothing to fix (check it by hand if KPP fails)" % f,
             lead=0, indent=6)
    elif m.group(2) == want:
        say("compile_wkc: tuv_kpp FIRST path already correct", "\u2713", lead=0, indent=6)
    else:
        open(f, "w").write(pat.sub(lambda mm: mm.group(1) + want, txt))
        say("compile_wkc: 'tuv_kpp FIRST %s' -> '%s' (WRF-Chem 4.7.x bug fix)" % (m.group(2), want),
            "\u2713", lead=0, indent=6)


def force_modern_gcc_flags(work):
    """./configure writes its own compiler flags into configure.wrf and those override the exported
    FCFLAGS/FFLAGS, so add -fallow-argument-mismatch (and -m64) there too.  Idempotent."""
    if S["dryrun"]:
        return
    cw = os.path.join(work, "configure.wrf")
    if not os.path.isfile(cw):
        return
    txt = open(cw).read()
    n = 0

    def add(m):
        nonlocal n
        line = m.group(0)
        missing = [f for f in MODERN_GCC_FLAGS.split() if f not in line]
        if not missing:
            return line
        n += 1
        return line.rstrip() + " " + " ".join(missing)

    txt = re.sub(r"(?m)^(FCBASEOPTS_NO_G|F77OPTS)\s*=.*$", add, txt)
    if n:
        open(cw, "w").write(txt)
        say("configure.wrf: %s added to %d Fortran flag line(s)" % (MODERN_GCC_FLAGS, n),
            "\u2713", lead=0, indent=6)
    elif not re.search(r"(?m)^(FCBASEOPTS_NO_G|F77OPTS)\s*=.*-fallow-argument-mismatch", txt):
        warn("no FCBASEOPTS_NO_G line found in %s; the flags (%s) are taken from the environment only"
             % (cw, MODERN_GCC_FLAGS), lead=0, indent=6)


def build_wrf(typ, ver):
    wd = wrf_dir(typ, ver)
    log = os.path.join(S["logdir"], "wrf_%s_%s.log" % (typ, ver))
    if os.path.isfile(os.path.join(wd, "main", "wrf.exe")) and not S["force"]:
        info("WRF %s (%s) already built (use --force to rebuild)" % (ver, type_label(typ)), lead=1)
        return
    info("Downloading and compiling WRF v%s (%s) - log: %s" % (ver, type_label(typ), log), lead=2)
    if typ == "chem":
        check_kpp_tools()
        if vtuple(ver) >= (4, 7):
            warn("WRF >= 4.7 has a known KPP build problem with recent GCC (wrf-model/wrf issue #2259:\n"
                 "'third body ... M' + one 'Error 127' per chemistry mechanism). This installer forces\n"
                 "the local byacc/flex into the build, which fixes most cases; if the compile still\n"
                 "fails, install WRF 4.6.x instead: %s --install 4.6.0 -chem --force" % EXE, lead=1)
    env = build_env(typ, ver)
    modern = chem_needs_modern_gcc(typ, ver)
    if modern:
        info("WRF-Chem %s with gfortran %d: adding %s (modern GCC compatibility)" % (ver, gfortran_major(), MODERN_GCC_FLAGS), lead=0)
    tarp = package(WRF_URL.format(v=ver), "WRFV%s.tar.gz" % ver)
    # unpack to a temporary name, build there (WRF dislikes being moved after configure)
    work = os.path.join(S["home"], "WRFV%s" % ver)
    unpack_single(tarp, work)
    if typ == "hydro":
        htar = package(HYDRO_URL, "wrf_hydro_v5.3.0.tar.gz")
        tmp = os.path.join(S["home"], "_hydro_tmp")
        unpack_single(htar, tmp)
        if not S["dryrun"]:
            src = os.path.join(tmp, "trunk", "NDHMS")
            run("cp -rf %s/* %s/hydro/" % (src, work), shell=True)
            rm(tmp)
    if not S["dryrun"]:
        rm(log)
    run(["./configure"], cwd=work, env=env, log=log,
        stdin="%s\n%s\n" % (S["wrf_opt"], S["nest_opt"]))
    if typ == "chem":
        force_kpp_tool_paths(work)      # configure.wrf YACC/FLEX -> local byacc/flex
        if modern:
            force_modern_gcc_flags(work)   # configure.wrf compile flags -> -fallow-argument-mismatch
        if typ == "chem" and vtuple(ver) >= (4, 7):
            fix_compile_wkc(work)             # WRF-Chem 4.7.x tuv_kpp output-path bug
    # old gfortran quirk handled in the original script
    gv = gfortran_major()
    cw = os.path.join(work, "configure.wrf")
    if not S["dryrun"] and 6 <= gv < 8 and os.path.isfile(cw):
        txt = [l for l in open(cw) if "-DBUILD_RRTMG_FAST=1" not in l]
        open(cw, "w").writelines(txt)
    target = "em_fire" if typ == "fireideal" else "em_real"
    needed = ["wrf.exe"] + (["ideal.exe"] if typ == "fireideal" else [])
    info("Compiling WRF %s (%s, ./compile %s) - this can take 20-40+ minutes" % (ver, type_label(typ), target), lead=0)
    run(["./compile", target], cwd=work, env=env, log=log, check=False)
    if not S["dryrun"]:
        bad = os.path.isfile(log) and "Problems building executables" in open(log, errors="replace").read()
        if bad or not all(os.path.isfile(os.path.join(work, "main", x)) for x in needed):
            if typ == "chem":
                S["ver_hint"] = ver
                kpp_hint(log, work)
            die("WRF %s compilation failed. See %s" % (type_label(typ), log))
        rm(wd)
        shutil.move(work, wd)
    ok("WRF %s built: %s" % (type_label(typ), wd))


def build_wps(typ, ver):
    pd = wps_dir(typ, ver)
    wd = wrf_dir(typ, ver)
    log = os.path.join(S["logdir"], "wps_%s_%s.log" % (typ, ver))
    if os.path.isfile(os.path.join(pd, "geogrid.exe")) and not S["force"]:
        info("WPS %s (%s) already built (use --force to rebuild)" % (ver, type_label(typ)), lead=1)
        return
    info("Downloading and compiling WPS v%s (%s) - log: %s" % (ver, type_label(typ), log), lead=2)
    env = build_env(typ)
    env["WRF_DIR"] = wd
    tarp = package(WPS_URL.format(v=ver), "WPSV%s.tar.gz" % ver)
    unpack_single(tarp, pd)
    if not S["dryrun"]:
        rm(log)
    run(["./clean"], cwd=pd, env=env, log=log, check=False)
    run(["./configure"], cwd=pd, env=env, log=log, stdin="%s\n" % S["wps_opt"])
    run(["./compile"], cwd=pd, env=env, log=log, check=False)
    if not S["dryrun"]:
        for exe in ("geogrid.exe", "ungrib.exe", "metgrid.exe"):
            if not os.path.isfile(os.path.join(pd, exe)):
                die("WPS %s compilation failed (%s missing). See %s" % (type_label(typ), exe, log))
        nl = os.path.join(pd, "namelist.wps")
        set_namelist_text(nl, "geog_data_path", "'%s/'" % S["geogdir"], single=True)
        if typ == "fire":
            setup_fire_wps(pd)
    ok("WPS %s built: %s" % (type_label(typ), pd))


# =============================================================================
#  WRF-Fire helpers
# =============================================================================
def setup_fire_wps(pd):
    """GEOGRID.TBL gets NFUEL_CAT and ZSF (subgrid=yes); namelist.wps gets subgrid_ratio_x/y."""
    gdir = os.path.join(pd, "geogrid")
    base = os.path.join(gdir, "GEOGRID.TBL.ARW")
    fire = os.path.join(gdir, "GEOGRID.TBL.ARW_FIRE")
    if not os.path.isfile(base):
        warn("%s not found - add NFUEL_CAT/ZSF to GEOGRID.TBL by hand (see the WRF-Fire chapter)." % base)
    else:
        txt = open(base).read().rstrip("\n")
        if "NFUEL_CAT" not in txt:
            last = [l for l in txt.split("\n") if l.strip()][-1]
            txt += "\n" + ("" if last.startswith("=") else _SEP + "\n") + FIRE_GEOGRID_ENTRIES
        else:
            txt += "\n"
        open(fire, "w").write(txt)
        link = os.path.join(gdir, "GEOGRID.TBL")
        rm(link)
        os.symlink("GEOGRID.TBL.ARW_FIRE", link)
        say("geogrid/GEOGRID.TBL -> GEOGRID.TBL.ARW_FIRE (NFUEL_CAT, ZSF on the fire subgrid)", "\u2713", lead=0, indent=6)
    add_wps_subgrid(os.path.join(pd, "namelist.wps"))


def add_wps_subgrid(nl):
    """Insert subgrid_ratio_x/y into &share of namelist.wps if missing."""
    if S["dryrun"] or not os.path.isfile(nl):
        return
    txt = open(nl).read()
    if re.search(r"^\s*subgrid_ratio_x", txt, re.I | re.M):
        return
    new, n = re.subn(r"^(\s*max_dom\s*=.*)$", r"\1\n subgrid_ratio_x = %d,\n subgrid_ratio_y = %d," % (FIRE_SR, FIRE_SR),
                     txt, count=1, flags=re.I | re.M)
    if n:
        open(nl, "w").write(new)
    else:
        warn("Could not find max_dom in %s - add subgrid_ratio_x/y = %d to &share yourself." % (nl, FIRE_SR))


def fire_prepare_domain(dom, rdir):
    """Make a run domain WRF-Fire ready: sr_x/sr_y, &fire block, namelist.fire, subgrid_ratio."""
    add_wps_subgrid(os.path.join(dom, "namelist.wps"))
    nl = os.path.join(dom, "namelist.input")
    if os.path.isfile(nl):
        txt = open(nl).read()
        if not re.search(r"^\s*sr_x\s*=", txt, re.I | re.M):
            txt = re.sub(r"^(\s*&domains\s*)$", r"\1\n sr_x = %d,\n sr_y = %d," % (FIRE_SR, FIRE_SR),
                         txt, count=1, flags=re.I | re.M)
        if not re.search(r"^\s*&fire", txt, re.I | re.M):
            txt = txt.rstrip("\n") + "\n\n" + FIRE_NAMELIST_INPUT
        open(nl, "w").write(txt)
    nf = os.path.join(dom, "namelist.fire")
    src = os.path.join(rdir, "namelist.fire")
    if os.path.islink(nf):
        os.remove(nf)
    if not os.path.isfile(nf) and os.path.isfile(src):
        shutil.copy2(src, nf)          # editable copy for custom fuel categories
    say("WRF-Fire settings added to the domain (sr_x/sr_y=%d, &fire, subgrid_ratio_x/y=%d)." % (FIRE_SR, FIRE_SR),
        "\u2713", lead=0, indent=6)


def setup_fire_geog():
    """Folders for the two extra WRF-Fire static datasets (not available from UCAR)."""
    info("Preparing WRF-Fire static data folders in %s" % S["geogdir"], lead=2)
    txt = ("WRF-Fire needs two datasets that are NOT part of the standard WPS_GEOG download:\n\n"
           "  NFUEL_CAT  Anderson 13 fuel categories (e.g. USGS LANDFIRE 'Anderson Fire Behavior Fuel Models')\n"
           "             -> convert to geogrid binary format and put it in  %s/landfire/\n"
           "  ZSF        high-resolution terrain height (1/3 or 1 arc-second DEM, e.g. USGS National Map)\n"
           "             -> convert to geogrid binary format and put it in  %s/highres_elev/\n\n"
           "Each folder needs an 'index' file (see the WPS chapter of the Users Guide for the geogrid format).\n"
           "GEOGRID.TBL of the Fire WPS build already points at these folders (rel_path, subgrid=yes).\n"
           "NOTE: halt_on_missing=yes - geogrid.exe stops if either dataset is missing for your domain.\n"
           % (S["geogdir"], S["geogdir"]))
    if not S["dryrun"]:
        for d in FIRE_GEOG_DIRS:
            os.makedirs(os.path.join(S["geogdir"], d), exist_ok=True)
        with open(os.path.join(S["geogdir"], "README_WRF_FIRE.txt"), "w") as f:
            f.write(txt)
    for d in FIRE_GEOG_DIRS:
        say("%s/%s/" % (S["geogdir"], d), "\u2713", lead=0, indent=6)
    print("\n".join("      " + l for l in txt.split("\n")))


def set_namelist_text(path, key, value, single=False):
    """Replace 'key = ...' in a namelist while preserving trailing comments."""
    if not os.path.isfile(path):
        return False
    lines = open(path).readlines()
    pat = re.compile(r"^(\s*%s\s*=\s*)(.*?)(\s*(?:!.*)?)(\r?\n?)$" % re.escape(key), re.I)
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if not m:
            continue
        old = m.group(2).strip()
        if single:
            vals = value
        else:
            count = max(1, old.count(",") + (0 if old.endswith(",") else 1))
            vals = ", ".join([value] * count) + ("," if old.endswith(",") else "")
        lines[i] = m.group(1) + vals + m.group(3) + m.group(4)
        open(path, "w").writelines(lines)
        return True
    return False


# =============================================================================
#  Geography data
# =============================================================================
def install_geog(extra=None, chem=False):
    names = list(extra or [])
    if chem:
        names += [n for n in CHEM_GEOGS if n not in names]
    if S["nogeog"] and not names:
        info("Skipping WPS_GEOG (--nogeog)")
        return
    info("Setting up terrestrial datasets in %s" % S["geogdir"], lead=2)
    if not S["nogeog"]:
        if os.path.isdir(S["geogdir"]) and not S["force"] and "geog_high_res_mandatory" not in names:
            say("Base datasets already present", "\u2713", lead=0, indent=6)
        else:
            tarp = package("%s/%s" % (GEOG_BASE, GEOG_MANDATORY[1]), GEOG_MANDATORY[1])
            unpack(tarp, S["home"])
    for n in names:
        if n == "geog_high_res_mandatory":
            continue
        d = os.path.join(S["geogdir"], n)
        if os.path.isdir(d) and not S["force"]:
            say("%s already present" % n, "\u2713", lead=0, indent=6)
            continue
        fn = "%s.tar.bz2" % n
        tarp = package("%s/%s" % (GEOG_BASE, fn), fn)
        os.makedirs(S["geogdir"], exist_ok=True) if not S["dryrun"] else None
        unpack(tarp, S["geogdir"])


# =============================================================================
#  Environment files, Do-List (SystemConfig / PrintGuidance in Build_WRF)
# =============================================================================
TCL_T = """#%Module1.0
## WRF @TYPE@ @VER@ environment - generated by wrfall_install.py
module-whatis "WRF @VER@ (@TYPE@) with local libraries"
set wrfhome @HOME@
set prefix @PREFIX@
setenv WRF_HOME $wrfhome
setenv WRF_DIR @WRFDIR@
setenv WPS_DIR @WPSDIR@
setenv WPS_GEOG $wrfhome/WPS_GEOG
setenv NPROCS @NPROCS@
setenv DIR $wrfhome
setenv PREFIX $prefix
setenv CC gcc
setenv CXX g++
setenv FC gfortran
setenv F77 gfortran
setenv NETCDF $prefix
setenv HDF5 $prefix
setenv JASPERLIB $prefix/lib
setenv JASPERINC $prefix/include
setenv FFLAGS "-O3 -fPIC"
setenv FCFLAGS "-O3 -fPIC"
@EXTRA@
prepend-path PATH $prefix/bin
prepend-path LD_LIBRARY_PATH $prefix/lib
"""

SH_T = """# WRF @TYPE@ @VER@ environment (bash) - source this file
export WRF_HOME=@HOME@
export WRF_DIR=@WRFDIR@
export WPS_DIR=@WPSDIR@
export WPS_GEOG=@HOME@/WPS_GEOG
export NPROCS=@NPROCS@
export DIR=@HOME@
export PREFIX=@PREFIX@
export CC=gcc CXX=g++ FC=gfortran F77=gfortran
export NETCDF=@PREFIX@
export HDF5=@PREFIX@
export JASPERLIB=@PREFIX@/lib
export JASPERINC=@PREFIX@/include
export FFLAGS="-O3 -fPIC"
export FCFLAGS="-O3 -fPIC"
@EXTRA@
export PATH=@PREFIX@/bin:$PATH
export LD_LIBRARY_PATH=@PREFIX@/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
"""

CSH_T = """# WRF @TYPE@ @VER@ environment (tcsh) - source this file
setenv WRF_HOME @HOME@
setenv WRF_DIR @WRFDIR@
setenv WPS_DIR @WPSDIR@
setenv WPS_GEOG @HOME@/WPS_GEOG
setenv NPROCS @NPROCS@
setenv DIR @HOME@
setenv PREFIX @PREFIX@
setenv CC gcc
setenv CXX g++
setenv FC gfortran
setenv F77 gfortran
setenv NETCDF @PREFIX@
setenv HDF5 @PREFIX@
setenv JASPERLIB @PREFIX@/lib
setenv JASPERINC @PREFIX@/include
setenv FFLAGS "-O3 -fPIC"
setenv FCFLAGS "-O3 -fPIC"
@EXTRA@
setenv PATH @PREFIX@/bin:${PATH}
if ($?LD_LIBRARY_PATH) then
    setenv LD_LIBRARY_PATH @PREFIX@/lib:${LD_LIBRARY_PATH}
else
    setenv LD_LIBRARY_PATH @PREFIX@/lib
endif
"""


def write_environment_files(typ, ver):
    lab = type_label(typ)
    extras = {
        "chem": dict(tcl="setenv WRF_CHEM 1\nsetenv WRF_KPP 1\nsetenv FLEX_LIB_DIR $prefix/lib\n"
                         "setenv FLEX $prefix/bin/flex\nsetenv YACC \"$prefix/bin/yacc -d\"",
                     sh="export WRF_CHEM=1\nexport WRF_KPP=1\nexport FLEX_LIB_DIR=@PREFIX@/lib\n"
                        "export FLEX=@PREFIX@/bin/flex\nexport YACC=\"@PREFIX@/bin/yacc -d\"",
                     csh="setenv WRF_CHEM 1\nsetenv WRF_KPP 1\nsetenv FLEX_LIB_DIR @PREFIX@/lib\n"
                         "setenv FLEX @PREFIX@/bin/flex\nsetenv YACC \"@PREFIX@/bin/yacc -d\""),
        "hydro": dict(tcl="setenv WRF_HYDRO 1", sh="export WRF_HYDRO=1", csh="setenv WRF_HYDRO 1"),
    }.get(typ, dict(tcl="", sh="", csh=""))
    if chem_needs_modern_gcc(typ, ver):
        fl = "-O3 -fPIC " + MODERN_GCC_FLAGS
        extras["tcl"] = extras["tcl"] + '\nsetenv FFLAGS "%s"\nsetenv FCFLAGS "%s"' % (fl, fl)
        extras["sh"] = extras["sh"] + '\nexport FFLAGS="%s"\nexport FCFLAGS="%s"' % (fl, fl)
        extras["csh"] = extras["csh"] + '\nsetenv FFLAGS "%s"\nsetenv FCFLAGS "%s"' % (fl, fl)
    subs = {"@HOME@": S["home"], "@PREFIX@": S["prefix"], "@WRFDIR@": wrf_dir(typ, ver),
            "@WPSDIR@": wps_dir(typ, ver), "@NPROCS@": str(S["nprocs"]), "@VER@": ver, "@TYPE@": lab}
    for fname, tmpl, key in (("WRF%s" % lab, TCL_T, "tcl"), ("WRF%s.sh" % lab, SH_T, "sh"),
                             ("WRF%s.csh" % lab, CSH_T, "csh")):
        txt = tmpl.replace("@EXTRA@", extras[key])
        for k, v in subs.items():
            txt = txt.replace(k, v)
        if not S["dryrun"]:
            with open(os.path.join(S["moddir"], fname), "w") as f:
                f.write(txt)
    say("Environment files written for %s" % lab, "\u2713", lead=0, indent=6)


def fire_guidance(types):
    L = []
    if "fire" in types:
        L += ["", "WRF-Fire (real data) - build type 'Fire':",
              "  1. Put NFUEL_CAT in %s/landfire/ and ZSF in %s/highres_elev/ (geogrid format)." % (S["geogdir"], S["geogdir"]),
              "  2. Create a domain:  %s --mkdomain NAME -fire" % EXE,
              "     (adds sr_x/sr_y=%d and a &fire block to namelist.input, subgrid_ratio_x/y to namelist.wps)" % FIRE_SR,
              "  3. Edit &fire: ifire=2 (level set), fire_fuel_read=-1 (fuel from WPS), ignition lines/times.",
              "     Grid settings in namelist.input must match namelist.wps; sr_x/sr_y = subgrid_ratio_x/y."]
    if "fireideal" in types:
        L += ["", "WRF-Fire (ideal cases) - build type 'FireIdeal' (compiled with ./compile em_fire):",
              "  cd <WRF-FireIdeal>/test/em_fire",
              "  ln -sf namelist.input_two_fires namelist.input ; ln -sf input_sounding_two_fires input_sounding",
              "  ./ideal.exe ; ./wrf.exe          (default case is hill_simple)"]
    return L


def write_dolist(types):
    have_mod = bool(which("modulecmd") or os.path.exists("/usr/bin/modulecmd") or os.environ.get("MODULESHOME"))
    L = ["Greetings, wonderful WRF owner.", "",
         "WRF was installed under: %s" % S["home"],
         "Build type(s): %s" % ", ".join(type_label(t) for t in types), "",
         "For you to use the system the environment must be set. Pick ONE build type",
         "at a time (WRFARW, WRFChem, WRFHydro, WRFFire or WRFFireIdeal) - do not load more than one together.", ""]
    first = type_label(types[0])
    if have_mod:
        L += ["### Option 1: Environment Modules (recommended) ###", "",
              " Bash (add to ~/.bashrc):", "",
              '   export MODULEPATH="${MODULEPATH}:%s"' % S["moddir"],
              "   module load WRF%s" % first, "",
              " Tcsh (add to ~/.cshrc):", "",
              "   setenv MODULEPATH ${MODULEPATH}:%s" % S["moddir"],
              "   module load WRF%s" % first, "",
              "### Option 2: Source the environment file ###"]
    else:
        L += ["The environment modules package (modulecmd) does not appear to be installed,",
              "so just source the environment files instead:"]
    L.append("")
    for t in types:
        lab = type_label(t)
        L += [" WRF-%s:" % lab, "   Bash : source %s/WRF%s.sh" % (S["moddir"], lab),
              "   Tcsh : source %s/WRF%s.csh" % (S["moddir"], lab)]
    L += fire_guidance(types)
    L += ["", "And while you are editing your login file, comment out or delete references to",
          "previous WRF environments, because they will come looking for you."]
    text = "\n".join(L) + "\n"
    if not S["dryrun"]:
        with open(os.path.expanduser("~/WRF-DoList.txt"), "w") as f:
            f.write(text)
    say("A special message from the WRF Overlord:", "*", lead=2)
    print("\n".join("    " + l for l in L))
    say("This information was also written to ~/WRF-DoList.txt", "\u2713", lead=1)


def print_cpu_info():
    i = S["isys"]
    info("Processor information dug up for %s:" % i["host"], lead=2)
    print("      HOSTNAME         : %s\n      SOCKETS          : %s\n      CORES PER SOCKET : %s\n      TOTAL CORES      : %s"
          % (i["host"], i["sockets"], i["cores_per_socket"], i["total_cores"]))
    if i["total_cores"] < 2:
        warn("Only %d core detected - WRF will be painfully slow. If that is wrong, set NPROCS in the module files after install." % i["total_cores"])
    if i["ht"]:
        warn("Hyper-threading appears to be ON. WRF runs most efficiently with one MPI rank per physical core;\n"
             "using more will probably degrade performance (run_wrf uses WRF_NP ranks - default 8, change it if needed).")


# =============================================================================
#  Flags / configuration (Build_WRF_ParseFlags + Build_WRF_Configure)
# =============================================================================
def build_parser():
    p = argparse.ArgumentParser(prog=EXE, add_help=False, allow_abbrev=False,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group
    a = g("primary")
    a.add_argument("--install", nargs="?", const="current", default=None, metavar="[RELEASE|list|listall]")
    a.add_argument("--update", nargs="?", const="current", default=None, metavar="[RELEASE|list]")
    a.add_argument("--addpack", action="append", default=[], metavar="PKG[,PKG]")
    a.add_argument("--addgeog", action="append", default=[], metavar="SET[,SET]")
    a.add_argument("--mkdomain", metavar="NAME")
    a.add_argument("--getdata", choices=["gfs", "ecmwf", "era5"])
    a.add_argument("--run", nargs="*", metavar="auto|YYYYMMDD CC HOURS")
    t = g("build types")
    for k in BUILD_TYPES:
        t.add_argument("-" + k, action="append_const", dest="types", const=k)
    t.add_argument("-all", action="store_true")
    t.add_argument("--type", action="append", dest="types_long", choices=BUILD_TYPES)
    o = g("options")
    o.add_argument("--homedir", metavar="DIR")
    o.add_argument("--repodir", metavar="DIR")
    o.add_argument("--import", dest="imports", action="append", default=[], metavar="DIR")
    for k in ("allyes", "continue", "force", "nolocal", "nounpack", "scrubpkgs", "norefresh",
              "nogeog", "curl", "wget", "scour", "debug", "dryrun", "version", "flags", "force_libs"):
        o.add_argument("--" + k.replace("_", "-"), dest=k, action="store_true")
    o.add_argument("--proxy", metavar="HOST:PORT")
    o.add_argument("--wrf-opt", default="34", help="WRF configure compiler option (default 34 = gfortran dmpar)")
    o.add_argument("--nest-opt", default="1")
    o.add_argument("--wps-opt", default="3", help="WPS configure option (default 3 = gfortran dmpar)")
    o.add_argument("--jobs", type=int)
    o.add_argument("--dl-jobs", type=int, default=8, metavar="N",
                   help="parallel HTTP range connections per download (default 8, 1 = single stream)")
    d = g("data and run")
    d.add_argument("--domain", metavar="DIR[,DIR2,...]",
                   help="domain name(s) under <home> or path(s), comma separated; default: current dir if it is a domain")
    d.add_argument("--date", metavar="YYYYMMDD")
    d.add_argument("--cycle", default="00")
    d.add_argument("--start", type=int, default=0)
    d.add_argument("--end", type=int)
    d.add_argument("--step", type=int, default=3)
    d.add_argument("--bbox", nargs=4, type=float, metavar=("W", "E", "S", "N"), default=None,
                   help="download area; default: the d01 extent of the domain's namelist.wps plus --margin")
    d.add_argument("--margin", type=float, default=1.0, metavar="DEG",
                   help="degrees added around the namelist.wps extent when downloading (default 1.0)")
    d.add_argument("--outdir", metavar="DIR")
    d.add_argument("--source", choices=["gfs", "ecmwf", "era5"], default="gfs")
    p.add_argument("-h", "--help", action="store_true")
    return p


FLAG_HELP = [
    ("--install [REL]", "Fresh install of WRF/WPS + libraries. REL may be list|listall|listgeog."),
    ("-arw -chem -hydro -fire -fireideal -all", "Build types (or --type arw|chem|hydro|fire|fireideal, repeatable).\n                                 -fire = WRF-Fire real-data build + fire-ready WPS; -fireideal = em_fire ideal cases;\n                                 -all = arw, chem, hydro, fire."),
    ("--update [REL]", "Build a newer release next to the current one, reuse libraries, refresh domains."),
    ("--addpack LIST", "Install optional packages: domwiz, scripts, cdsapi."),
    ("--addgeog LIST", "Install terrestrial datasets: chem, mandatory, fire (creates landfire/ + highres_elev/ folders), or any UCAR dataset name."),
    ("--nogeog", "Do not download the base WPS_GEOG dataset."),
    ("--import DIR", "Import domains (dirs holding namelist.wps) from an older installation."),
    ("--scour", "Delete the previous release directories after a successful install/update."),
    ("--continue", "Resume: skip libraries already built."),
    ("--force", "Rebuild WRF/WPS even if present; allow installing an earlier release. Support libraries already installed are kept."),
    ("--force-libs", "Also rebuild the support libraries (zlib, hdf5, netcdf, mpich, ..., flex, byacc)."),
    ("--homedir DIR", "Top-level install directory (default $WRF_HOME or ~/Build_WRF)."),
    ("--repodir DIR", "Take package tarfiles from a local repository instead of the network."),
    ("--nolocal", "Ignore tarfiles in <home>/packages and download fresh copies."),
    ("--nounpack", "Download only; do not unpack or build."),
    ("--scrubpkgs", "Delete package tarfiles after installation (default: keep them)."),
    ("--norefresh", "With --update, do not refresh existing domain directories."),
    ("--curl / --wget", "Force curl or wget for downloads (default: auto = wget, then curl, then built-in urllib)."),
    ("--dl-jobs N", "Parallel HTTP range connections per file (default 8; 1 = single stream). Big speed-up for\n                                 the WPS_GEOG/Chem tarballs and ECMWF data; --getdata is capped at 3 to stay polite."),
    ("--proxy HOST:PORT", "Set http_proxy/https_proxy (proxies are cleared by default)."),
    ("--wrf-opt N --wps-opt N", "Override the configure menu choices (defaults 34 and 3, gfortran dmpar)."),
    ("--allyes", "Answer YES to every question (for automation)."),
    ("--dryrun", "Show what would be done without downloading or building."),
    ("--mkdomain NAME", "Create a run domain under <home>/NAME (from setup_wrf_domain.sh); add -fire for a WRF-Fire domain."),
    ("--getdata gfs|ecmwf|era5", "Download initial/boundary data; see --date --cycle --start --end --step --margin --bbox --domain.\n                                 The area comes from the domain's namelist.wps (d01 lat/lon extent + --margin)."),
    ("--run auto|YYYYMMDD CC HH [gfs|ecmwf|era5]", "Run geogrid(opt)/ungrib/metgrid/real/wrf for --domain (from run_wrf.sh); source defaults to --source (gfs)."),
    ("--debug  --version  --flags  --help", "Housekeeping."),
]


def print_flags():
    say("AVAILABLE FLAGS - BECAUSE YOU ASKED NICELY AND I'M BEGINNING TO LIKE YOU", "", lead=1, trail=1)
    for f, d in FLAG_HELP:
        print("    %-30s %s" % (f, d))
    print()
    sys.exit(1)


def print_help():
    say("USAGE:  %% %s [primary flag] [build type] [additional options]" % EXE, "", lead=1, trail=1)
    print("""    RUDIMENTARY INFORMATION OF THE BASIC KIND:

    To do a default installation of the current release:

       %% %(e)s --install -arw                 (or -chem, -hydro, -fire, -all)

    To install WRF-Fire (real-data build, or ideal em_fire cases):

       %% %(e)s --install -fire
       %% %(e)s --install -fireideal
       %% %(e)s --mkdomain firedom -fire

    To list the releases you can install:

       %% %(e)s --install list

    To update an existing installation:

       %% %(e)s --update

    To install an optional package or terrestrial dataset:

       %% %(e)s --addpack domwiz,scripts,cdsapi
       %% %(e)s --addgeog chem

    Daily-use helpers carried over from your automation scripts:

       %% %(e)s --mkdomain ethiowrf
       %% %(e)s --getdata gfs --domain ~/Build_WRF/ethiowrf --date 20261016 --end 72
       %% %(e)s --run auto --domain ~/Build_WRF/ethiowrf

    For the full flag list:  %% %(e)s --flags
""" % {"e": EXE})
    sys.exit(1)


def configure(args):
    S["continue"] = getattr(args, "continue")
    S.update(
        allyes=args.allyes, force=args.force, force_libs=getattr(args, "force_libs", False),
        nolocal=args.nolocal, nounpack=args.nounpack, scrubpkgs=args.scrubpkgs,
        refresh=not args.norefresh, nogeog=args.nogeog, scour=args.scour,
        debug=args.debug, dryrun=args.dryrun, wrf_opt=args.wrf_opt,
        nest_opt=args.nest_opt, wps_opt=args.wps_opt, args=args)
    # Proxy handling: OFF by default (Build_WRF), ON with --proxy
    for k in ("http_proxy", "https_proxy"):
        os.environ.pop(k, None)
    if args.proxy:
        pr = args.proxy.rstrip("/") + "/"
        os.environ["http_proxy"] = "http://" + pr
        os.environ["https_proxy"] = "https://" + pr
    os.environ["LC_ALL"] = "C"

    # download method
    if args.curl and args.wget:
        die("Pick one of --curl or --wget, not both.")
    S["method"] = "curl" if args.curl else "wget" if args.wget else "auto"
    S["dl_jobs"] = max(1, min(int(args.dl_jobs or 8), DL_MAX_JOBS))

    # home
    home = args.homedir or os.environ.get("WRF_HOME") or ""
    if not home:                       # running from inside a domain (./wrfall ...): use its recorded home
        home = domain_meta(os.getcwd()).get("home", "")
    prev = os.path.expanduser("~/Build_WRF")
    if not home:
        if args.install is not None and not args.allyes:
            say("Finding a perfect home - Location, Location, ..., then Simulation", "*", lead=1)
            home = ask_value("Where would you like to install WRF?", prev)
        else:
            home = prev
    home = re.sub(r"/+$", "", home) or prev
    set_paths(home)

    # repository
    S["repodir"] = ""
    if args.repodir:
        r = os.path.abspath(os.path.expanduser(args.repodir))
        if not os.path.isdir(r):
            die("You passed --repodir %s but that directory does not exist." % args.repodir)
        S["repodir"] = r

    # build types
    types = list(args.types or []) + list(args.types_long or [])
    if args.all:
        types = list(ALL_TYPES)
    seen = set()
    S["types"] = [t for t in types if not (t in seen or seen.add(t))]

    S["isys"] = collect_system_info()
    S["nprocs"] = S["isys"]["total_cores"] or os.cpu_count() or 4
    S["makej"] = args.jobs or min(8, os.cpu_count() or 4)


def sanity_check(args):
    primary = [x for x in (args.install is not None, args.update is not None) if x]
    if len(primary) > 1:
        die("This is not a \"McSloppy Burger\", so you can't have it \"Your Way!\"\n\n"
            "It's either \"--install\" or \"--update\", but not both.")
    if os.uname().machine in ("i386", "i686"):
        die("This installer targets 64-bit Linux. This system appears to be 32-bit.")
    if sys.platform != "linux" and not args.dryrun:
        warn("This installer is written for Linux; expect surprises on %s." % sys.platform)
    if args.update is not None and (args.addpack or args.addgeog):
        die("\"--addpack\" and \"--addgeog\" are stand-alone options; they can't be mixed with \"--update\".")


# =============================================================================
#  --install
# =============================================================================
def list_releases(mode):
    inst = read_release(S["home"])
    cur = inst.get("wrfems", "")
    rels = available_releases()
    info("Installed release : %s" % (cur or "No WRF installed"), lead=1)
    info("Summary of available WRF/WPS releases", lead=1)
    for i, r in enumerate(rels):
        tag = "  <- Most current" if i == 0 else ""
        if r == cur:
            tag = "  <- Currently installed"
        print("        V%-8s%s" % (r, tag))
    print()
    if mode == "listgeog" or mode == "listall":
        info("Terrestrial datasets available", lead=1)
        print("        %-28s %s" % ("geog_high_res_mandatory", "Base WPS_GEOG (installed by default)"))
        for n in CHEM_GEOGS:
            print("        %-28s %s" % (n, "WRF-Chem dataset (group: chem)"))
        print("        %-28s %s" % ("fire", "WRF-Fire folders landfire/ + highres_elev/ (you supply NFUEL_CAT, ZSF)"))
    if mode == "listall":
        info("Optional packages", lead=1)
        for k, v in ADDPACKS.items():
            print("        %-10s %s" % (k, v))
    print()
    info("%s" % ("No need for an update at this time" if cur == rels[0] else "You now have the power - Abuse it wisely!"), lead=0)
    sys.exit(0)


def resolve_release(req, allow_current_default=True):
    if req in ("list", "listall", "listgeog"):
        list_releases(req)
    rels = available_releases()
    if req == "current":
        return rels[0]
    if not re.match(r"^\d+\.\d+(\.\d+)?$", req):
        die("Fantasy Release: %s\n\nA release looks like %s." % (req, DEFAULT_RELEASE))
    return req


def do_install(args):
    types = S["types"]
    if args.install in ("list", "listall", "listgeog"):
        list_releases(args.install)
    if not types:
        die("You must choose at least one build type:\n"
            "    For WRF-ARW   : %s --install -arw\n"
            "    For WRF-Chem  : %s --install -chem\n"
            "    For WRF-Hydro : %s --install -hydro\n"
            "    For WRF-Fire  : %s --install -fire   (or -fireideal for em_fire ideal cases)\n"
            "    Or all of them: %s --install -all" % (EXE, EXE, EXE, EXE, EXE))
    rel = resolve_release(args.install)
    inst = read_release(S["home"])
    irel = inst.get("wrfems", "")
    already = [t for t in types if os.path.isfile(os.path.join(wrf_dir(t, rel), "main", "wrf.exe"))
               and (t in NO_WPS_TYPES or os.path.isfile(os.path.join(wps_dir(t, rel), "geogrid.exe")))]
    if irel == rel and len(already) == len(types) and not (S["force"] or S["continue"]):
        say("Build_WRF-style notice: release %s (%s) is already installed.\nPass \"--force\" to rebuild it or \"--continue\" to resume." % (
            rel, ", ".join(type_label(t) for t in types)), "!", lead=2)
        return
    if irel and vtuple(rel) < vtuple(irel) and not S["force"]:
        die("Installation Faux Paux: %s > %s\n\nYou are attempting to install an earlier release than the one that\n"
            "currently resides here. If you are true about your intent, use \"--force\"." % (irel, rel))

    info("Preparing to install WRF/WPS V%s for: %s" % (rel, ", ".join(type_label(t) for t in types)), lead=1)
    check_prereqs()
    est = disk_estimate_gb(types, S["nogeog"], "chem" in types)
    info("Rough disk space needed: about %d GB (planning figure; check the dataset sizes you actually select)." % est, lead=1)
    parent = S["home"] if os.path.isdir(S["home"]) else os.path.dirname(S["home"]) or "."
    try:
        free = shutil.disk_usage(parent).free / 1e9
        if free < est:
            warn("Only %.0f GB free on %s - that may not be enough." % (free, parent))
    except OSError:
        pass
    print_cpu_info()
    info(format_system_info(S["isys"]), lead=1)

    if not ask("Installing WRF %s. Is this OK by you?" % rel, "OK by me"):
        say("Maybe we can try again later - Bye", "", lead=1)
        return
    make_dirs()
    S["stamp"] = rel

    build_libraries()
    for t in types:
        build_wrf(t, rel)
        if t not in NO_WPS_TYPES:
            build_wps(t, rel)
    # geography first so the namelist path exists; reapply namelist path afterwards
    if any(t not in NO_WPS_TYPES for t in types):
        install_geog(chem="chem" in types)
    if "fire" in types:
        setup_fire_geog()
    if not S["nounpack"]:
        for t in types:
            write_environment_files(t, rel)
        if not S["dryrun"]:
            write_release(S["home"], rel, types)
        # Domain Wizard comes with the original installer
        if not os.path.isdir(os.path.join(scripts_dir(), "WRF-domain-Wizard")):
            do_addpack_domwiz(quiet=True)
        write_dolist(types)
        import_domains(args.imports, rel)
        if S["scour"] and irel and irel != rel:
            scour_old(irel, inst.get("types", types))
    info("Installation complete - Now go forth and simulate!", lead=2)
    for t in types:
        if t == "fireideal":
            print("      WRF-%s: %s/main/{wrf,ideal}.exe  (test/em_fire)" % (type_label(t), wrf_dir(t, rel)))
            continue
        print("      WRF-%s: %s/main/{wrf,real,ndown,tc}.exe" % (type_label(t), wrf_dir(t, rel)))
        print("      WPS-%s: %s/{geogrid,ungrib,metgrid}.exe" % (type_label(t), wps_dir(t, rel)))
    print("\n      Environment files : %s\n      Logs              : %s\n" % (S["moddir"], S["logdir"]))


def scour_old(old, types):
    info("Scouring release %s (--scour)" % old, lead=1)
    for t in types:
        rm(wrf_dir(t, old))
        rm(wps_dir(t, old))


# =============================================================================
#  --update
# =============================================================================
def do_update(args):
    inst = read_release(S["home"])
    irel = inst.get("wrfems", "")
    if not irel:
        die("It appears that you are putting the cart before the horse. I am unable to find a current\n"
            "installation under %s, which is necessary when using \"--update\".\n"
            "Pass --homedir or set WRF_HOME." % S["home"])
    if args.update in ("list", "listall"):
        list_releases(args.update)
    types = S["types"] or inst.get("types") or ["arw"]
    target = resolve_release(args.update)
    if vtuple(target) <= vtuple(irel) and not S["force"]:
        say("No need to update - You are current (V%s), and beautiful!" % irel, "\u263a", lead=2)
        return
    info("Updating V%s -> V%s for: %s" % (irel, target, ", ".join(type_label(t) for t in types)), lead=1)
    info("Libraries are reused; the new release is built next to the old one.", lead=0)
    check_prereqs()
    if not ask("Is this OK by you?", "OK by me"):
        say("Maybe we can try again later - Bye", "", lead=1)
        return
    make_dirs()
    build_libraries()                     # a no-op unless libraries are missing/--force
    for t in types:
        build_wrf(t, target)
        if t not in NO_WPS_TYPES:
            build_wps(t, target)
        if not S["nounpack"]:
            write_environment_files(t, target)
    if S["nounpack"]:
        return
    write_release(S["home"], target, types)
    if S["refresh"]:
        refresh_domains(target, types)
    write_dolist(types)
    if S["scour"]:
        scour_old(irel, types)
    info("The system update is complete - Now go forth and simulate!", lead=2)


# =============================================================================
#  --addpack / --addgeog
# =============================================================================
def split_args(lst):
    out = []
    for item in lst:
        out += [x.strip().lower() for x in re.split(r"[,;\s]+", item) if x.strip()]
    return out


def do_addpack_domwiz(quiet=False):
    dest = os.path.join(scripts_dir(), "WRF-domain-Wizard")
    if not S["dryrun"]:
        os.makedirs(scripts_dir(), exist_ok=True)
    if os.path.isdir(os.path.join(dest, ".git")) and not S["force"]:
        if not quiet:
            say("Domain Wizard already installed", "\u2713", lead=0)
        return
    if not which("git"):
        warn("git not found - skipping Domain Wizard download")
        return
    info("Downloading WRF Domain Wizard", lead=1)
    rm(dest)
    run(["git", "clone", "--depth", "1", DOMWIZ_REPO, dest], check=False)


def do_addpack_scripts():
    d = scripts_dir()
    if not S["dryrun"]:
        os.makedirs(d, exist_ok=True)
    info("Saving your original automation scripts in %s" % d, lead=1)
    for f in UTIL_SCRIPTS:
        dest = os.path.join(d, f)
        if os.path.isfile(dest) and not S["force"]:
            say("%-30s found locally" % f, "\u2713", lead=0, indent=6)
            continue
        acquire(UTIL_RAW.format(f=f), dest)
        if not S["dryrun"]:
            os.chmod(dest, 0o755)
        say("%-30s downloaded" % f, "\u2713", lead=0, indent=6)


def do_addpack_cdsapi():
    info("Installing the cdsapi Python module (pip --user)", lead=1)
    run([sys.executable, "-m", "pip", "install", "--user", "cdsapi"], check=False)
    if not os.path.isfile(os.path.expanduser("~/.cdsapirc")):
        warn("ERA5 downloads also need your CDS credentials in ~/.cdsapirc\n"
             "(see the Copernicus Climate Data Store 'How to API' page).")


def do_addpack(args):
    packs = split_args(args.addpack)
    geogs = split_args(args.addgeog)
    inst = read_release(S["home"])
    if not inst and not S["dryrun"]:
        die("I'm unable to determine the release of the current installation under %s,\n"
            "which is needed when processing \"--addpack\"/\"--addgeog\". Pass --homedir or set WRF_HOME." % S["home"])
    make_dirs()
    todo = []
    for p in packs:
        p = ADDPACK_ALIASES.get(p, p)
        if p not in ADDPACKS:
            die("Unknown package \"%s\". Available: %s" % (p, ", ".join(ADDPACKS)))
        todo.append(p)
    if todo:
        info("Installing %d optional package(s): %s" % (len(todo), ", ".join(todo)), lead=1)
        for p in dict.fromkeys(todo):
            {"domwiz": do_addpack_domwiz, "scripts": do_addpack_scripts, "cdsapi": do_addpack_cdsapi}[p]()
    if geogs:
        names = []
        for g in geogs:
            names += ADDGEOG_GROUPS.get(g, [g])
        names = list(dict.fromkeys(names))
        known = set(CHEM_GEOGS) | {"geog_high_res_mandatory"}
        unk = [n for n in names if n not in known]
        if unk:
            warn("Not in my built-in list, will try %s/<name>.tar.bz2: %s" % (GEOG_BASE, ", ".join(unk)))
        if names:
            install_geog(extra=names)
        if "fire" in geogs:
            setup_fire_geog()
    info("Optional package installation complete", lead=1)


# =============================================================================
#  Domains: import / refresh / create
# =============================================================================
def find_domains(root):
    out = []
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        return out
    if os.path.isfile(os.path.join(root, "namelist.wps")) and os.path.isfile(os.path.join(root, "namelist.input")):
        return [root]
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "namelist.wps")) \
                and os.path.isfile(os.path.join(p, "namelist.input")):
            out.append(p)
    return out


def relink_domain(dom, typ, ver):
    """Point the executables in a domain at the given release (setup_wrf_domain.sh behaviour)."""
    wps, run_dir = wps_dir(typ, ver), os.path.join(wrf_dir(typ, ver), "run")
    for src in glob.glob(wps + "/*.exe") + [wps + "/link_grib.csh"] + glob.glob(run_dir + "/*.exe"):
        if os.path.exists(src):
            dst = os.path.join(dom, os.path.basename(src))
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(src, dst)
    for sub in ("geogrid", "ungrib", "metgrid"):
        s, d = os.path.join(wps, sub), os.path.join(dom, sub)
        if os.path.isdir(s):
            rm(d)
            shutil.copytree(s, d, symlinks=True)
    # link everything else from run/ that the domain doesn't have (cp -as in the original)
    for ent in os.listdir(run_dir):
        d = os.path.join(dom, ent)
        if not os.path.lexists(d):
            os.symlink(os.path.join(run_dir, ent), d)


def refresh_domains(ver, types):
    doms = [d for d in find_domains(S["home"]) if os.path.isfile(os.path.join(d, ".wrfems_domain"))]
    if not doms:
        return
    info("Refreshing %d run-time domain(s)" % len(doms), lead=1)
    for d in doms:
        meta = json.load(open(os.path.join(d, ".wrfems_domain")))
        typ = meta.get("type", types[0])
        if typ not in types:
            typ = types[0]
        stamp = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        for nl in ("namelist.wps", "namelist.input"):
            if os.path.isfile(os.path.join(d, nl)):
                shutil.copy2(os.path.join(d, nl), os.path.join(d, "%s.%s.bak" % (nl, stamp)))
        if not S["dryrun"]:
            relink_domain(d, typ, ver)
            set_namelist_text(os.path.join(d, "namelist.wps"), "geog_data_path", "'%s/'" % S["geogdir"], single=True)
            meta.update(version=ver)
            json.dump(meta, open(os.path.join(d, ".wrfems_domain"), "w"))
        say("Refreshed %s (your namelists were kept; backups saved as *.bak)" % os.path.basename(d), "\u2713", lead=0, indent=6)


def import_domains(imports, ver):
    cand = []
    for i in imports:
        cand += find_domains(i)
    if not cand:
        return
    info("Importing %d requested run-time domain(s) into %s" % (len(cand), S["home"]), lead=1)
    types = read_release(S["home"]).get("types") or ["arw"]
    for src in dict.fromkeys(cand):
        name = os.path.basename(src)
        dst = os.path.join(S["home"], name)
        if os.path.exists(dst):
            warn("Not imported: domain \"%s\" already exists under %s" % (name, dst))
            continue
        if run(["rsync", "-aq", src + "/", dst + "/"], check=False) != 0:
            warn("Import failed for %s" % src)
            continue
        if not S["dryrun"]:
            json.dump({"type": types[0], "version": ver}, open(os.path.join(dst, ".wrfems_domain"), "w"))
            relink_domain(dst, types[0], ver)
            set_namelist_text(os.path.join(dst, "namelist.wps"), "geog_data_path", "'%s/'" % S["geogdir"], single=True)
        say("Mission \"Import %s\" Accomplished!" % name, "\u2713", lead=0, indent=6)


SRC_DIRS = {"gfs": "GFS_Input", "ecmwf": "ECMWF_Input", "era5": "ERA5_Input"}
VTABLES = {"gfs": "Vtable.GFS", "ecmwf": "Vtable.ECMWF", "era5": "Vtable.ERA-interim.pl"}
OUT_SUBS = ("metgrid", "ungrib", "Plots", "geogrid")   # <domain>/wrf_output/<sub>
UTIL_REPO = "https://github.com/goikelem/Automation-Utility-Scripts"


def domain_meta(dom):
    try:
        with open(os.path.join(dom, ".wrfems_domain")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def resolve_domains(spec):
    """--domain a,b,c  (names under <home>, relative or absolute paths).
    With no --domain, the current directory is used when it is a domain
    (this is what makes './wrfall --getdata gfs ...' work from inside a domain)."""
    out = []
    names = [x for x in re.split(r"[,;\s]+", spec or "") if x]
    if not names:
        if os.path.isfile(os.path.join(os.getcwd(), ".wrfems_domain")):
            return [os.getcwd()]
        return []
    for nm in names:
        p = os.path.abspath(os.path.expanduser(nm))
        if not os.path.isdir(p):
            alt = os.path.join(S["home"], nm)
            if os.path.isdir(alt):
                p = alt
        out.append(p)
    return list(dict.fromkeys(out))


def date_dir(dom, source, date, cyc):
    """<domain>/GFS_Input/YYYYMMDDCC/  (note: a real sub-directory, not a name prefix)."""
    return os.path.join(dom, SRC_DIRS[source], "%s%s" % (date, cyc))


def make_domain_dirs(dom):
    for sub in SRC_DIRS.values():
        os.makedirs(os.path.join(dom, sub), exist_ok=True)
    for sub in OUT_SUBS:
        os.makedirs(os.path.join(dom, "wrf_output", sub), exist_ok=True)


def scripts_dir():
    """$Build_WRF/Scripts : one shared folder for all domains."""
    return os.path.join(S["home"], "Scripts")


def setup_scripts():
    """<home>/Scripts gets *.sh and *.py from Automation-Utility-Scripts and a clone of
    WRF-domain-Wizard (Scripts/WRF-domain-Wizard). Created once, shared by every domain."""
    sd = scripts_dir()
    if not S["dryrun"]:
        os.makedirs(sd, exist_ok=True)
    have = [f for f in UTIL_SCRIPTS if os.path.isfile(os.path.join(sd, f))]
    if len(have) == len(UTIL_SCRIPTS) and not S["force"]:
        say("%s already filled" % sd, "\u2713", lead=0, indent=6)
    else:
        info("Filling %s" % sd, lead=0)
        got = False
        if which("git") and not S["dryrun"]:
            tmp = tempfile.mkdtemp(prefix=".utils_", dir=sd)
            try:
                if run(["git", "clone", "--depth", "1", UTIL_REPO, os.path.join(tmp, "r")], check=False) == 0:
                    for pat in ("*.sh", "*.py"):
                        for f in glob.glob(os.path.join(tmp, "r", "**", pat), recursive=True):
                            dst = os.path.join(sd, os.path.basename(f))
                            shutil.copy2(f, dst)
                            os.chmod(dst, 0o755)
                            got = True
            finally:
                rm(tmp)
        elif S["dryrun"]:
            say("would git clone %s and copy *.sh/*.py into %s" % (UTIL_REPO, sd), "dbg", lead=0, indent=6)
            got = True
        if not got:                                   # no git / clone failed: raw files
            for f in UTIL_SCRIPTS:
                dest = os.path.join(sd, f)
                if not (os.path.isfile(dest) and not S["force"]):
                    try:
                        acquire(UTIL_RAW.format(f=f), dest)
                    except SystemExit:
                        warn("Could not fetch %s" % f)
                        continue
                    if not S["dryrun"]:
                        os.chmod(dest, 0o755)
    do_addpack_domwiz(quiet=True)


def link_wrfall(dom):
    """Soft link <domain>/wrfall -> this script, so it can be run from the domain."""
    me = os.path.realpath(sys.argv[0])
    dst = os.path.join(dom, "wrfall")
    if os.path.lexists(dst):
        os.remove(dst)
    os.symlink(me, dst)


def detect_builds():
    """Scan <home> for finished builds: WPS-<ver>-<Label>/geogrid.exe + WRF-<ver>-<Label>/main/wrf.exe.
    Returns {type: [versions, newest first]}. Works even when builds were made one at a time,
    at different versions (e.g. Chem/Hydro 4.6.0 and Fire 4.7.0), or without a matching .release entry."""
    label2typ = {v: k for k, v in TYPE_LABELS.items()}
    out = {}
    for d in glob.glob(os.path.join(S["home"], "WPS-*-*")):
        m = re.match(r"^WPS-(\d+(?:\.\d+)*)-(\w+)$", os.path.basename(d))
        if not m or m.group(2) not in label2typ:
            continue
        ver, typ = m.group(1), label2typ[m.group(2)]
        if typ in NO_WPS_TYPES:
            continue
        if os.path.isfile(os.path.join(d, "geogrid.exe")) and \
                os.path.isfile(os.path.join(wrf_dir(typ, ver), "main", "wrf.exe")):
            out.setdefault(typ, []).append(ver)
    return {t: sorted(v, key=vtuple, reverse=True) for t, v in out.items()}


def pick_build(built, inst):
    """Choose (type, version) for --mkdomain: the type given on the command line, else the only
    build that exists. With several builds and no type flag, list them and stop."""
    wanted = S["types"]
    if wanted and wanted[0] in NO_WPS_TYPES:
        die("-%s has no WPS; run its ideal cases from <WRF dir>/test/em_fire. Use -fire for real-data domains." % wanted[0])
    if wanted:
        typ = wanted[0]
    elif len(built) == 1:
        typ = next(iter(built))
    elif built:
        lines = "\n".join("      -%-6s  (WRF/WPS %s)" % (t, built[t][0]) for t in BUILD_TYPES if t in built)
        die("More than one build is installed under %s:\n%s\n"
            "    Tell me which one this domain is for, e.g.:  %s --mkdomain %s -%s"
            % (S["home"], lines, EXE, S["mkdomain_name"], next(iter(built))))
    else:
        typ = "arw"
    if typ not in built:
        have = ", ".join("-%s (%s)" % (t, built[t][0]) for t in built) or "nothing"
        die("%s has not been built. Install it first.\n    Builds found under %s: %s"
            % (type_label(typ), S["home"], have))
    rel = inst.get("wrfems") if inst else None
    ver = rel if rel in built[typ] else built[typ][0]
    return typ, ver


def do_mkdomain(args):
    """Port of setup_wrf_domain.sh (does NOT edit ~/.bashrc; writes <domain>/domain_env.sh instead)."""
    inst = read_release(S["home"])
    S["mkdomain_name"] = args.mkdomain
    built = detect_builds()                      # {type: [versions, newest first]} found on disk
    if not inst and not built:
        die("No installation found under %s. Run --install first (or pass --homedir)." % S["home"])
    typ, ver = pick_build(built, inst)
    name = args.mkdomain
    if not re.match(r"^[A-Za-z0-9._-]+$", name):
        die("Domain names may only contain letters, digits, dot, dash and underscore.")
    dom = os.path.join(S["home"], name)
    if not os.path.isfile(os.path.join(wps_dir(typ, ver), "geogrid.exe")):
        die("%s %s has not been built. Install it first." % (type_label(typ), ver))
    os.makedirs(dom, exist_ok=True)
    wps, rdir = wps_dir(typ, ver), os.path.join(wrf_dir(typ, ver), "run")
    relink_domain(dom, typ, ver)
    for nl, src in (("namelist.wps", wps), ("namelist.input", rdir)):
        d = os.path.join(dom, nl)
        if os.path.islink(d):
            os.remove(d)
        if not os.path.isfile(d):
            shutil.copy2(os.path.join(src, nl), d)
    make_domain_dirs(dom)
    if typ == "fire":
        fire_prepare_domain(dom, rdir)
    json.dump({"type": typ, "version": ver, "home": S["home"]}, open(os.path.join(dom, ".wrfems_domain"), "w"))
    with open(os.path.join(dom, "domain_env.sh"), "w") as f:
        f.write('export WRF_HOME="%s"\nexport WRF_WPS="%s"\nexport WRF_RUN="%s"\nexport DOMAIN_DIR="%s"\n'
                % (S["home"], wps, rdir, dom))
    link_wrfall(dom)
    setup_scripts()
    ok("Domain ready: %s" % dom)
    print("      Layout: GFS_Input/ ECMWF_Input/ ERA5_Input/ (each holds YYYYMMDDCC/), wrf_output/{metgrid,ungrib,Plots,geogrid}/,\n"
          "              wrfall -> %s\n"
          "      Shared scripts (utility scripts + WRF-domain-Wizard): %s\n"
          "      Edit namelist.wps first (e.g. with WRF-domain-Wizard): --getdata reads its lat/lon extent\n"
          "      and downloads only that area (+ --margin degrees).\n"
          "      Work from inside the domain:\n"
          "        cd %s\n"
          "        ./wrfall --getdata gfs --date YYYYMMDD --cycle 00 --end 72\n"
          "        ./wrfall --run auto gfs\n"
          "      Load its variables with:  source %s/domain_env.sh\n" % (os.path.realpath(sys.argv[0]), scripts_dir(), dom, dom))


# =============================================================================
#  --getdata  (download_025_gfs.sh / download_ecmwf_ifs_025.sh / download_era5.*)
# =============================================================================
# -----------------------------------------------------------------------------
#  Domain extent from namelist.wps  (min/max latitude and longitude of d01)
# -----------------------------------------------------------------------------
_R_EARTH = 6370000.0          # WPS uses a sphere of this radius


def nl_values(path, key):
    """First &geogrid/&share line 'key = a, b, ...' -> list of strings (quotes/comments stripped)."""
    pat = re.compile(r"^\s*%s\s*=\s*(.*?)\s*(?:!.*)?$" % re.escape(key), re.I)
    for ln in open(path):
        m = pat.match(ln)
        if m:
            return [v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()]
    return []


def _wrap180(x):
    return (x + 180.0) % 360.0 - 180.0


def namelist_extent(nl_path):
    """Return dict(minlat, maxlat, minlon, maxlon, proj) for domain 1 of a namelist.wps,
    using the same map projections as WPS (lambert, polar, mercator, lat-lon)."""
    from math import radians as rad, degrees as deg, sin, cos, tan, atan, atan2, log, exp, sqrt, pi

    def num(key, default=None):
        v = nl_values(nl_path, key)
        try:
            return float(v[0])
        except (IndexError, ValueError):
            if default is None:
                raise ValueError("'%s' not found in %s" % (key, nl_path))
            return default

    proj = (nl_values(nl_path, "map_proj") or ["lambert"])[0].lower()
    ref_lat, ref_lon = num("ref_lat"), num("ref_lon")
    t1 = num("truelat1", ref_lat)
    t2 = num("truelat2", t1)
    stand = num("stand_lon", ref_lon)
    dx, dy = num("dx"), num("dy", num("dx"))
    nx, ny = int(num("e_we")) - 1, int(num("e_sn")) - 1          # mass-grid cells
    ref_x = num("ref_x", (nx + 1) / 2.0)                         # grid index (1 = first mass point)
    ref_y = num("ref_y", (ny + 1) / 2.0)
    if proj == "lat-lon" and abs(num("pole_lat", 90.0) - 90.0) > 1e-6:
        warn("namelist.wps uses a rotated lat-lon pole; extent is approximate")

    R = _R_EARTH
    if proj == "lambert":
        p1, p2 = rad(t1), rad(t2)
        if abs(t1 - t2) < 1e-9:
            n = sin(p1)
        else:
            n = log(cos(p1) / cos(p2)) / log(tan(pi / 4 + p2 / 2) / tan(pi / 4 + p1 / 2))
        F = cos(p1) * tan(pi / 4 + p1 / 2) ** n / n
        rho = lambda phi: R * F / tan(pi / 4 + phi / 2) ** n
        rho0 = R * F
        fwd = lambda lat, lon: (rho(rad(lat)) * sin(n * rad(_wrap180(lon - stand))),
                                rho0 - rho(rad(lat)) * cos(n * rad(_wrap180(lon - stand))))

        def inv(x, y):
            sg = 1.0 if n > 0 else -1.0
            r = sg * sqrt(x * x + (rho0 - y) ** 2)
            th = atan2(sg * x, sg * (rho0 - y))
            return deg(2 * atan((R * F / r) ** (1.0 / n)) - pi / 2), _wrap180(stand + deg(th / n))
    elif proj == "polar":
        north = t1 >= 0
        k = R * (1 + sin(rad(abs(t1))))
        def fwd(lat, lon):
            ph = rad(lat if north else -lat)
            r = k * tan(pi / 4 - ph / 2)
            dl = rad(_wrap180(lon - stand))
            return (r * sin(dl), (-r * cos(dl)) if north else (r * cos(dl)))

        def inv(x, y):
            r = sqrt(x * x + y * y)
            ph = pi / 2 - 2 * atan(r / k)
            return (deg(ph) if north else -deg(ph)), _wrap180(stand + deg(atan2(x, -y if north else y)))
    elif proj == "mercator":
        c = cos(rad(t1))
        fwd = lambda lat, lon: (R * c * rad(_wrap180(lon - stand)), R * c * log(tan(pi / 4 + rad(lat) / 2)))
        inv = lambda x, y: (deg(2 * atan(exp(y / (R * c))) - pi / 2), _wrap180(stand + deg(x / (R * c))))
    elif proj == "lat-lon":
        fwd = lambda lat, lon: (rad(lon) * R, rad(lat) * R)          # dx, dy are degrees: scale below
        inv = lambda x, y: (deg(y / R), deg(x / R))
        dx, dy = rad(dx) * R, rad(dy) * R
    else:
        raise ValueError("unsupported map_proj '%s'" % proj)

    xr, yr = fwd(ref_lat, ref_lon)
    x0, x1 = xr - (ref_x - 0.5) * dx, xr + (nx + 0.5 - ref_x) * dx          # outer cell edges
    y0, y1 = yr - (ref_y - 0.5) * dy, yr + (ny + 0.5 - ref_y) * dy
    pts = []
    for i in range(101):                                                      # sample the whole boundary
        f = i / 100.0
        xs, ys = x0 + f * (x1 - x0), y0 + f * (y1 - y0)
        pts += [inv(xs, y0), inv(xs, y1), inv(x0, ys), inv(x1, ys)]
    lats = [q[0] for q in pts]
    lons = [q[1] for q in pts]
    if max(lons) - min(lons) > 180:                                            # crosses +/-180
        lons = [l % 360 for l in lons]
    return dict(minlat=min(lats), maxlat=max(lats), minlon=min(lons), maxlon=max(lons), proj=proj)


def download_bbox(dom, margin):
    """(W, E, S, N, text) for --getdata: namelist.wps extent + margin, snapped outward to 0.25 deg."""
    from math import floor, ceil
    nl = os.path.join(dom, "namelist.wps")
    ext = namelist_extent(nl)
    q = lambda v, f: f(v / 0.25) * 0.25
    w = q(ext["minlon"] - margin, floor)
    e = q(ext["maxlon"] + margin, ceil)
    s_ = max(-90.0, q(ext["minlat"] - margin, floor))
    n_ = min(90.0, q(ext["maxlat"] + margin, ceil))
    txt = ("Domain extent (namelist.wps, d01, %s): lat %.2f to %.2f, lon %.2f to %.2f\n"
           "Download area (+%.2f deg margin): lat %.2f to %.2f, lon %.2f to %.2f"
           % (ext["proj"], ext["minlat"], ext["maxlat"], ext["minlon"], ext["maxlon"],
              margin, s_, n_, w, e))
    return w, e, s_, n_, txt



def fetch_binary(url, out, tries=5):
    """Download one GRIB file: streamed to disk (no whole-file buffering),
    resumable, parallel ranges capped at 3 (NOMADS throttles aggressive clients)."""
    part = out + ".part"
    jobs = max(1, min(int(S.get("dl_jobs", 8) or 8), 3))
    for n in range(1, tries + 1):
        try:
            if _download_urllib(url, part, jobs=jobs) == 0 and os.path.isfile(part):
                with open(part, "rb") as f:
                    magic = f.read(4)
                if magic == b"GRIB":
                    if sys.stdout.isatty():
                        sys.stdout.write("\n")
                    os.replace(part, out)
                    return True
                rm(part)
                return False      # server answered, but no GRIB: not available yet
        except Exception:
            pass
        time.sleep(5 * n)
    return False


ERA5_SFC_VARS = ["10m_u_component_of_wind", "10m_v_component_of_wind", "2m_dewpoint_temperature",
                 "2m_temperature", "land_sea_mask", "mean_sea_level_pressure", "sea_ice_cover",
                 "sea_surface_temperature", "skin_temperature", "snow_depth", "soil_type",
                 "surface_pressure", "volumetric_soil_water_layer_1", "volumetric_soil_water_layer_2",
                 "volumetric_soil_water_layer_3", "volumetric_soil_water_layer_4",
                 "soil_temperature_level_1", "soil_temperature_level_2",
                 "soil_temperature_level_3", "soil_temperature_level_4"]
ERA5_PL_VARS = ["geopotential", "relative_humidity", "specific_humidity", "temperature",
                "u_component_of_wind", "v_component_of_wind"]
ERA5_LEVELS = ["1", "2", "3", "5", "7", "10", "20", "30", "50", "70", "100", "125", "150", "175", "200",
               "225", "250", "300", "350", "400", "450", "500", "550", "600", "650", "700", "750", "775",
               "800", "825", "850", "875", "900", "925", "950", "975", "1000"]


def getdata_one(args, dom):
    date = args.date or dt.datetime.utcnow().strftime("%Y%m%d")
    cyc = args.cycle
    end = args.end if args.end is not None else (21 if args.getdata == "era5" else 120)
    if args.bbox:
        w, e, s, n = args.bbox
        info("Download area from --bbox: lat %.2f to %.2f, lon %.2f to %.2f" % (s, n, w, e), lead=1)
    elif dom and os.path.isfile(os.path.join(dom, "namelist.wps")):
        try:
            w, e, s, n, txt = download_bbox(dom, args.margin)
        except (ValueError, OSError) as ex:
            die("Could not read the domain extent from %s/namelist.wps: %s\n(or pass --bbox W E S N)" % (dom, ex))
        info(txt, lead=1)
    else:
        w, e, s, n = 20.0, 54.0, -10.0, 25.0
        warn("No domain namelist.wps found - using the default area lat %.0f to %.0f, lon %.0f to %.0f\n"
             "(use --domain DIR or --bbox W E S N)" % (s, n, w, e), lead=1)
    if args.outdir:
        out = os.path.abspath(os.path.expanduser(args.outdir))
    elif dom:
        out = date_dir(dom, args.getdata, date, cyc)          # <domain>/GFS_Input/YYYYMMDD00/
    else:
        out = os.path.join(S["home"], {"gfs": "GFS_025", "ecmwf": "ECMWF_IFS_025", "era5": "ERA5_DATA"}[args.getdata],
                           "%s%s" % (date, cyc))
    if not S["dryrun"]:
        os.makedirs(out, exist_ok=True)
    info("%s %s %sZ f%03d-f%03d every %dh -> %s" % (args.getdata.upper(), date, cyc, args.start, end, args.step, out), lead=1)
    failed = 0

    if args.getdata == "gfs":
        base = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
        vars_ = "HGT ICEC LAND MSLET PRES RH SNOD SOILW TMP TSOIL UGRD VGRD WEASD".split()
        plevs = ("1000 975 950 925 900 850 800 750 700 650 600 550 500 450 400 350 300 250 200 150 100 70 50 40 30 20 "
                 "15 10 7 5 3 2 1 0.7 0.4 0.2 0.1 0.07 0.04 0.02 0.01").split()
        other = ("2_m_above_ground 10_m_above_ground surface mean_sea_level 0-0.1_m_below_ground "
                 "0.1-0.4_m_below_ground 0.4-1_m_below_ground 1-2_m_below_ground max_wind tropopause").split()
        qs = "".join("&var_%s=on" % v for v in vars_) + "".join("&lev_%s_mb=on" % l for l in plevs) \
            + "".join("&lev_%s=on" % l for l in other)
        gw, ge = w % 360.0, e % 360.0                       # NOMADS grid runs 0..360
        if gw > ge:                                         # area straddles the prime meridian
            gw, ge = 0.0, 360.0
        qs += "&subregion=&leftlon=%s&rightlon=%s&toplat=%s&bottomlat=%s&dir=%%2Fgfs.%s%%2F%s%%2Fatmos" % (gw, ge, n, s, date, cyc)
        for h in range(args.start, end + 1, args.step):
            f = "gfs.t%sz.pgrb2.0p25.f%03d" % (cyc, h)
            o = os.path.join(out, "%s%s_%s" % (date, cyc, f))
            if os.path.isfile(o) and os.path.getsize(o) and not S["force"]:
                say("[skip] %s" % os.path.basename(o), "", lead=0, indent=6)
                continue
            say("[get ] %s" % os.path.basename(o), "", lead=0, indent=6)
            if S["dryrun"] or fetch_binary("%s?file=%s%s" % (base, f, qs), o):
                continue
            failed += 1
            say("[FAIL] %s (data not yet available or request error)" % os.path.basename(o), "!", lead=0, indent=6)
            time.sleep(2)

    elif args.getdata == "ecmwf":
        base = "https://data.ecmwf.int/forecasts"
        for h in range(args.start, end + 1, args.step):
            f = "%s%s0000-%dh-oper-fc.grib2" % (date, cyc, h)
            o = os.path.join(out, "%s%s_%s" % (date, cyc, f))
            if os.path.isfile(o) and os.path.getsize(o) and not S["force"]:
                say("[skip] %s" % os.path.basename(o), "", lead=0, indent=6)
                continue
            say("[get ] %s" % os.path.basename(o), "", lead=0, indent=6)
            url = "%s/%s/%sz/ifs/0p25/oper/%s" % (base, date, cyc, f)
            if S["dryrun"] or fetch_binary(url, o):
                continue
            failed += 1
            say("[FAIL] %s" % os.path.basename(o), "!", lead=0, indent=6)
            time.sleep(2)

    else:  # era5: one request pair per calendar day covered by cycle + [start, end] hours
        t0 = dt.datetime.strptime(date + cyc, "%Y%m%d%H")
        days = {}
        for h in range(args.start, end + 1, args.step):
            t = t0 + dt.timedelta(hours=h)
            days.setdefault(t.strftime("%Y%m%d"), []).append("%02d:00" % t.hour)
        if S["dryrun"]:
            for d, hrs in days.items():
                say("would request ERA5 sfc+pl for %s (%s) via cdsapi" % (d, ",".join(hrs)), "dbg", lead=0, indent=6)
        else:
            try:
                import cdsapi
            except ImportError:
                die("The cdsapi module is missing. Run:  %s --addpack cdsapi" % EXE)
            area = [n, w, s, e]
            c = cdsapi.Client()
            for d, hrs in sorted(days.items()):
                sfc = os.path.join(out, "ERA5_%s_sfc.grib" % d)
                pl = os.path.join(out, "ERA5_%s_pl.grib" % d)
                req = {"product_type": "reanalysis", "format": "grib", "year": d[:4], "month": d[4:6],
                       "day": d[6:], "time": hrs, "area": area}
                for dest, ds, extra in ((sfc, "reanalysis-era5-single-levels", {"variable": ERA5_SFC_VARS}),
                                        (pl, "reanalysis-era5-pressure-levels",
                                         {"variable": ERA5_PL_VARS, "pressure_level": ERA5_LEVELS})):
                    if os.path.isfile(dest) and os.path.getsize(dest) and not S["force"]:
                        say("[skip] %s" % os.path.basename(dest), "", lead=0, indent=6)
                        continue
                    say("[get ] %s" % os.path.basename(dest), "", lead=0, indent=6)
                    try:
                        c.retrieve(ds, dict(req, **extra), dest)
                    except Exception as ex:
                        failed += 1
                        say("[FAIL] %s: %s" % (os.path.basename(dest), ex), "!", lead=0, indent=6)
    info("Done. Failed files: %d" % failed, lead=1)
    if args.getdata == "era5":
        print("      --run handles the ERA5 Vtable/prefix steps (PRES + SFC ungrib passes, fg_name) for you.\n")


def do_getdata(args):
    date = args.date or dt.datetime.utcnow().strftime("%Y%m%d")
    if not re.match(r"^\d{8}$", date):
        die("--date must be YYYYMMDD")
    if args.cycle not in ("00", "06", "12", "18"):
        die("--cycle must be 00, 06, 12 or 18")
    doms = resolve_domains(args.domain)
    if args.domain and not doms:
        die("No domain found for --domain %s" % args.domain)
    for d in doms:
        if not os.path.isdir(d):
            die("Domain directory does not exist: %s" % d)
    for d in (doms or [""]):
        getdata_one(args, d)


# =============================================================================
#  --run  (run_wrf.sh)
# =============================================================================
def archive_outputs(base, cdate, kind):
    """Keep step products under <domain>/wrf_output/{geogrid,ungrib,metgrid}/."""
    if S["dryrun"]:
        return
    root = os.path.join(base, "wrf_output", kind)
    if kind == "geogrid":
        dst = root
    else:
        dst = os.path.join(root, cdate)
    os.makedirs(dst, exist_ok=True)
    if kind == "geogrid":
        for f in glob.glob(os.path.join(base, "geo_em.d*.nc")) + [os.path.join(base, "log.geogrid"),
                                                                    os.path.join(base, "geogrid.log")]:
            if os.path.isfile(f):
                shutil.copy2(f, dst)
    elif kind == "ungrib":
        for f in [os.path.join(base, "log.ungrib"), os.path.join(base, "ungrib.log")] + \
                 glob.glob(os.path.join(base, "PRES:*")) + glob.glob(os.path.join(base, "SFC:*")) + \
                 glob.glob(os.path.join(base, "FILE:*")):
            if os.path.isfile(f):
                shutil.move(f, os.path.join(dst, os.path.basename(f)))
    else:
        for f in [os.path.join(base, "log.metgrid"), os.path.join(base, "metgrid.log")] + \
                 glob.glob(os.path.join(base, "met_em*")):
            if os.path.isfile(f):
                if os.path.basename(f).startswith("met_em"):
                    shutil.move(f, os.path.join(dst, os.path.basename(f)))
                else:
                    shutil.copy2(f, dst)


def set_vtable(base, vtable):
    vt = os.path.join(base, "Vtable")
    if S["dryrun"]:
        return
    if os.path.lexists(vt):
        os.remove(vt)
    os.symlink("ungrib/Variable_Tables/" + vtable, vt)     # ln -sf ungrib/Variable_Tables/<Vtable> Vtable


def link_grib(base, env, files):
    for old in glob.glob(os.path.join(base, "GRIBFILE.*")):
        os.remove(old)
    step(["./link_grib.csh"] + files, base, env, "log.link_grib")


def parse_run_args(r, default_source):
    """--run auto [gfs|ecmwf|era5]   or   --run YYYYMMDD CC HOURS [gfs|ecmwf|era5]"""
    r = list(r or ["auto"])
    src = default_source
    if r and r[-1].lower() in SRC_DIRS:
        src = r.pop().lower()
    if not r or r == ["auto"]:
        return dt.datetime.utcnow().strftime("%Y%m%d"), "00", 72, src
    if len(r) == 3:
        return r[0], r[1], r[2], src
    die("Usage: --run auto [gfs|ecmwf|era5]   or   --run YYYYMMDD CYCLE FORECAST_HOURS [gfs|ecmwf|era5]")


def do_run(args):
    doms = resolve_domains(args.domain)
    if not doms:
        die("--run needs --domain DIR[,DIR2,...] (or run it from inside a domain created with --mkdomain).")
    in_date, cc, fc, src = parse_run_args(args.run, args.source)
    if not re.match(r"^\d{8}$", in_date) or cc not in ("00", "06", "12", "18") or not str(fc).isdigit() \
            or not (1 <= int(fc) <= 72):
        die("Bad arguments: date YYYYMMDD, cycle 00|06|12|18, hours 1-72.")
    for d in doms:
        if not os.path.isdir(d):
            die("Run directory does not exist: %s" % d)
    for d in doms:
        run_one(d, in_date, cc, int(fc), src)


def run_one(base, in_date, cc, fc, src):
    start = dt.datetime.strptime(in_date + cc, "%Y%m%d%H")
    end = start + dt.timedelta(hours=fc)
    cdate = in_date + cc
    gribdir = date_dir(base, src, in_date, cc)
    wrfout = os.path.join(base, "wrf_output", cdate)
    real_np, wrf_np = int(os.environ.get("REAL_NP", 2)), int(os.environ.get("WRF_NP", 8))

    for exe in ("ungrib.exe", "metgrid.exe", "real.exe", "wrf.exe", "link_grib.csh"):
        if not os.path.exists(os.path.join(base, exe)):
            die("Required file not found in the domain: %s" % exe)
    if not os.path.isdir(gribdir) and not S["dryrun"]:
        die("GRIB input directory not found: %s\n(download it with --getdata %s --domain %s --date %s --cycle %s)"
            % (gribdir, src, base, in_date, cc))
    if not S["dryrun"]:
        for sub in OUT_SUBS:
            os.makedirs(os.path.join(base, "wrf_output", sub), exist_ok=True)
    env = build_env(None)
    mpi = os.path.join(S["prefix"], "bin", "mpirun")
    mpi = mpi if os.path.exists(mpi) else (which("mpirun") or "mpirun")
    t0 = time.time()
    info("WRF run started in %s. Source=%s, date=%s, cycle=%s UTC, forecast=%d hours"
         % (os.path.basename(base), src, in_date, cc, fc), lead=1)

    def stamp(d):
        return d.strftime("%Y-%m-%d_%H:00:00")

    wpsnl, wrfnl = os.path.join(base, "namelist.wps"), os.path.join(base, "namelist.input")
    for f in (wpsnl, wrfnl):
        shutil.copy2(f, "%s.bak.%s" % (f, dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")))
    for k, v in (("start_date", "'%s'" % stamp(start)), ("end_date", "'%s'" % stamp(end))):
        if not set_namelist_text(wpsnl, k, v):
            die("namelist key '%s' not found in %s" % (k, wpsnl))
    if os.environ.get("RUN_GEOGRID") == "1":
        info("Running geogrid.exe", lead=0)
        step(["./geogrid.exe"], base, env, "log.geogrid")
        archive_outputs(base, cdate, "geogrid")
    else:
        info("Skipping geogrid.exe (set RUN_GEOGRID=1 to run it)", lead=0)

    files = sorted(f for f in glob.glob(os.path.join(gribdir, "*")) if os.path.isfile(f))
    if not files and not S["dryrun"]:
        die("No GRIB files found in %s" % gribdir)
    for old in glob.glob(os.path.join(base, "PRES:*")) + glob.glob(os.path.join(base, "SFC:*")) + \
            glob.glob(os.path.join(base, "FILE:*")):
        if not S["dryrun"]:
            os.remove(old)

    info("Linking GRIB files and Vtable", lead=0)
    if src == "era5":
        # pressure-level and surface files are ungribbed separately (prefix PRES / SFC)
        pl = [f for f in files if "_pl" in os.path.basename(f)] or files
        sf = [f for f in files if "_sfc" in os.path.basename(f)]
        passes = [("PRES", pl, os.environ.get("ERA5_VTABLE_PL", "Vtable.ERA-interim.pl"))]
        if sf:
            passes.append(("SFC", sf, os.environ.get("ERA5_VTABLE_SFC", "Vtable.ERA-interim.pl")))
        for prefix, flist, vt in passes:
            link_grib(base, env, flist)
            set_vtable(base, vt)
            if not S["dryrun"]:
                set_namelist_text(wpsnl, "prefix", "'%s'" % prefix, single=True)
            info("Running ungrib.exe (%s)" % prefix, lead=0)
            step(["./ungrib.exe"], base, env, "log.ungrib")
            if not S["dryrun"] and os.path.isfile(os.path.join(base, "log.ungrib")):
                shutil.copy2(os.path.join(base, "log.ungrib"), os.path.join(base, "log.ungrib.%s" % prefix))
        if not S["dryrun"]:
            set_namelist_text(wpsnl, "fg_name", "'PRES', 'SFC'" if sf else "'PRES'", single=True)
    else:
        link_grib(base, env, files)
        set_vtable(base, VTABLES[src])
        if not S["dryrun"]:
            set_namelist_text(wpsnl, "prefix", "'FILE'", single=True)
            set_namelist_text(wpsnl, "fg_name", "'FILE'", single=True)
        info("Running ungrib.exe", lead=0)
        step(["./ungrib.exe"], base, env, "log.ungrib")
    info("Running metgrid.exe", lead=0)
    step(["./metgrid.exe"], base, env, "log.metgrid")
    if not S["dryrun"]:
        # real.exe needs met_em* in the run directory: copy them there, keep the originals
        # (and the ungrib intermediates) archived under wrf_output/{ungrib,metgrid}/<cycle>/
        for f in glob.glob(os.path.join(base, "met_em*")):
            os.makedirs(os.path.join(base, "wrf_output", "metgrid", cdate), exist_ok=True)
            shutil.copy2(f, os.path.join(base, "wrf_output", "metgrid", cdate, os.path.basename(f)))
        for f in [os.path.join(base, "log.metgrid")]:
            if os.path.isfile(f):
                os.makedirs(os.path.join(base, "wrf_output", "metgrid", cdate), exist_ok=True)
                shutil.copy2(f, os.path.join(base, "wrf_output", "metgrid", cdate))
    archive_outputs(base, cdate, "ungrib")

    info("Updating namelist.input", lead=0)
    for k, v in (("run_hours", fc), ("start_year", start.year), ("start_month", start.month),
                 ("start_day", start.day), ("start_hour", start.hour), ("end_year", end.year),
                 ("end_month", end.month), ("end_day", end.day), ("end_hour", end.hour)):
        if not set_namelist_text(wrfnl, k, str(v)):
            die("namelist key '%s' not found in %s" % (k, wrfnl))
    info("Running real.exe with %d MPI rank(s)" % real_np, lead=0)
    step([mpi, "-np", str(real_np), "./real.exe"], base, env, "log.real")
    info("Running wrf.exe with %d MPI rank(s)" % wrf_np, lead=0)
    step([mpi, "-np", str(wrf_np), "./wrf.exe"], base, env, "log.wrf")

    if not S["dryrun"]:
        os.makedirs(wrfout, exist_ok=True)
        outs = glob.glob(os.path.join(base, "wrfout_*"))
        if not outs:
            warn("No wrfout_* files found in %s" % base)
        for f in outs:
            shutil.move(f, os.path.join(wrfout, os.path.basename(f).replace(":", "_").replace("-", "_")))
        for f in glob.glob(os.path.join(base, "met_em*")):      # working copies; archived copies remain
            os.remove(f)
    el = int(time.time() - t0)
    ok("WRF run completed. Output: %s  (elapsed %dh %dm %ds)" % (wrfout, el // 3600, el % 3600 // 60, el % 60), lead=1)


def step(cmd, cwd, env, logname):
    log = os.path.join(cwd, logname)
    if not S["dryrun"]:
        rm(log)
    run(cmd, cwd=cwd, env=env, log=log)


# =============================================================================
#  Main
# =============================================================================
def _actions(args):
    """(present participle phrase, past phrase) for each task requested, in execution order."""
    acts = []
    if args.install is not None:
        acts.append(("installing WRF/WPS", "installed WRF/WPS"))
    if args.update is not None:
        acts.append(("updating WRF/WPS", "updated WRF/WPS"))
    if args.addpack or args.addgeog:
        acts.append(("adding optional packages/geography data", "added the optional packages/geography data"))
    if args.mkdomain:
        acts.append(("creating the domain '%s'" % args.mkdomain, "created the domain '%s'" % args.mkdomain))
    if args.getdata:
        t = args.getdata.upper()
        acts.append(("downloading %s data" % t, "downloaded the %s data" % t))
    if args.run is not None:
        acts.append(("running WRF", "run WRF"))
    return acts


def welcome_text(args):
    acts = _actions(args)
    if not acts:
        return "Welcome to WRFall."
    return "Welcome to WRFall. You are %s." % " and ".join(a[0] for a in acts)


def closing_text(args):
    acts = _actions(args)
    return "WRFall finished. You have %s." % " and ".join(a[1] for a in acts)


def main():
    signal.signal(signal.SIGINT, on_sigint)
    p = build_parser()
    args, extra = p.parse_known_args()
    if len(sys.argv) == 1 or args.help:
        print_help()
    if args.flags:
        print_flags()
    if args.version:
        say("What you have is the WRF installation and update tool (V%s)" % VERSION, "", lead=1, trail=2)
        return
    if extra:
        die("What, exactly, do you expect to achieve by passing such gibberish (%s)?" % " ".join(extra))

    sanity_check(args)
    configure(args)

    say(welcome_text(args), "", lead=1, trail=1)

    did = False
    if args.install is not None:
        did = True
        do_install(args)
    if args.update is not None:
        did = True
        do_update(args)
    if args.addpack or args.addgeog:
        did = True
        do_addpack(args)
    if args.mkdomain:
        did = True
        do_mkdomain(args)
    if args.getdata:
        did = True
        do_getdata(args)
    if args.run is not None:
        did = True
        do_run(args)
    if not did:
        die("What do you want from me?\n\n    %s --install -arw\n    %s --update\n    %s --addpack domwiz\n\n"
            "Try \"%s --help\"." % (EXE, EXE, EXE, EXE))
    say(closing_text(args), "", lead=1, trail=2)


if __name__ == "__main__":
    main()
