<h1 align="center">📘 WRFall User Guide</h1>

<p align="center">
  <b>From a bare Linux account to a running WRF forecast, step by step.</b><br>
  <i>Covers installation, updates, domains, data download, runs, WRF-Fire and WRF-Chem.</i>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/guide%20for-WRFall%201.4.2-ff6b6b?style=for-the-badge" alt="version">
  <img src="https://img.shields.io/badge/python-3.6%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="python">
  <img src="https://img.shields.io/badge/no%20root-required-f39c12?style=for-the-badge" alt="no root">
</p>

---

## 📑 Table of contents

1. [What WRFall does](#1--what-wrfall-does)
2. [Before you begin](#2--before-you-begin)
3. [Getting WRFall from GitHub](#3--getting-wrfall-from-github)
4. [Your first command: listing releases](#4--your-first-command-listing-releases)
5. [Choosing where everything lives](#5--choosing-where-everything-lives)
6. [Installing WRF and WPS](#6--installing-wrf-and-wps)
7. [Loading the environment](#7--loading-the-environment)
8. [Terrestrial (geography) data](#8--terrestrial-geography-data)
9. [Optional packages](#9--optional-packages)
10. [Creating a domain](#10--creating-a-domain)
11. [Downloading forecast data](#11--downloading-forecast-data)
12. [Running WRF](#12--running-wrf)
13. [Updating to a newer release](#13--updating-to-a-newer-release)
14. [Importing domains from an old installation](#14--importing-domains-from-an-old-installation)
15. [Network, proxies and offline installs](#15--network-proxies-and-offline-installs)
16. [WRF-Fire](#16--wrf-fire)
17. [WRF-Chem](#17--wrf-chem)
18. [WRF-Hydro](#18--wrf-hydro)
19. [Complete flag reference](#19--complete-flag-reference)
20. [Troubleshooting and FAQ](#20--troubleshooting-and-faq)
21. [Command cheat sheet](#21--command-cheat-sheet)

---

## 1. 🌍 What WRFall does

**WRFall** is one Python script, `wrfall_install.py`, that handles the whole life of a WRF setup without root access:

| Stage | What WRFall does for you |
|---|---|
| 🔧 **Install** | Builds the support libraries (zlib, HDF5, NetCDF-C, NetCDF-Fortran, MPICH, libpng, JasPer, flex, byacc), then WRF and WPS |
| 🔄 **Update** | Builds a newer release next to the old one and reuses the libraries |
| 🗺️ **Domains** | Creates run directories with all links, folders and namelists in place |
| 📡 **Data** | Downloads GFS, ECMWF IFS or ERA5 data for exactly the area of your domain |
| ▶️ **Run** | Runs ungrib, metgrid, real and wrf, and archives the products |

It uses only the Python standard library. The one exception is ERA5 downloads, which need the `cdsapi` module.

---

## 2. 🧰 Before you begin

### Operating system and tools

WRFall targets **64-bit Linux**. You need these tools on the machine:

| Tool | Needed for |
|---|---|
| `gcc`, `g++`, `gfortran` | Compiling everything (required) |
| `make`, `tar`, `perl` | Building and unpacking (required) |
| `tcsh` or `csh` | WPS compile and `link_grib.csh` (strongly recommended) |
| `m4` | Some library builds (recommended) |
| `git` | Cloning WRFall, WRF-domain-Wizard and your utility scripts (recommended) |
| `wget` or `curl` | Faster downloads than the built-in downloader (recommended) |

If a required tool is missing, WRFall stops and tells you what to install. Ask your administrator, or use your package manager if you have rights:

```bash
# Debian / Ubuntu
sudo apt install build-essential gfortran perl tcsh m4 git wget

# RHEL / Rocky / Alma
sudo dnf groupinstall 'Development Tools'
sudo dnf install gcc-gfortran perl tcsh m4 git wget
```

### Disk space

WRFall prints a planning estimate before it starts. The rough figures it uses:

| Item | Approx. size |
|---|:---:|
| Support libraries | 2 GB |
| Each build type (WRF + WPS) | 4 GB |
| Base WPS_GEOG dataset | 30 GB |
| Extra WRF-Chem datasets | 12 GB |

So one ARW build with the base geography data needs about **36 GB**. These are planning figures, not guarantees.

### CPU cores and memory

WRFall reports your sockets, cores and memory at install time. It warns you if only one core is detected, and if hyper-threading seems to be on (WRF is usually most efficient with one MPI rank per physical core).

---

## 3. 📥 Getting WRFall from GitHub

The project lives at **https://github.com/goikelem/WRFall**. Pick whichever method suits you.

### Option A: clone the repository (recommended)

```bash
cd ~
git clone https://github.com/goikelem/WRFall.git
cd WRFall
chmod +x wrfall_install.py
```

Later, `git pull` inside the folder gets you the newest version of the script.

### Option B: download only the script

```bash
# with wget
wget https://raw.githubusercontent.com/goikelem/WRFall/main/wrfall_install.py

# or with curl
curl -LO https://raw.githubusercontent.com/goikelem/WRFall/main/wrfall_install.py

chmod +x wrfall_install.py
```

> ⚠️ Use the **raw** address (`raw.githubusercontent.com`). The page at `github.com/.../blob/main/...` is an HTML viewer, and saving it gives you a web page rather than the script.

### Option C: download a ZIP

On the repository page click **Code → Download ZIP**, then:

```bash
unzip WRFall-main.zip
cd WRFall-main
chmod +x wrfall_install.py
```

### Check that it works

```bash
./wrfall_install.py --version
python3 --version        # must be 3.6 or newer
./wrfall_install.py --help
```

If `./wrfall_install.py` says *Permission denied*, you forgot `chmod +x`. You can always run it as `python3 wrfall_install.py ...` instead.

> 💡 In this guide, `./wrfall_install.py` and `./wrfall` mean the same program. Inside a domain you get a `wrfall` soft link to the script (see [section 10](#10--creating-a-domain)).

---

## 4. 🔎 Your first command: listing releases

Before installing, see which WRF releases are available:

```bash
./wrfall_install.py --install list
```

Real output from a machine that already has WRF 4.7.0:

```text
    *  Finding a perfect home - Location, Location, ..., then Simulation
    Where would you like to install WRF? [/home/goitomk/Build_WRF]:

    Welcome to WRFall. You are installing WRF/WPS.

    *  Installed release : 4.7.0

    *  Summary of available WRF/WPS releases
        V4.8.0     <- Most current
        V4.7.1
        V4.7.0     <- Currently installed
        V4.6.1
        V4.6.0
        V4.5.2
        V4.5.1
        V4.5
        ...
        V4.1
        V4.0.3
        V4.0.2
        V4.0.1
        V4.0
```

### How to read it

| Line | Meaning |
|---|---|
| `Where would you like to install WRF? [...]` | WRFall asks for your **home directory** (the top-level install folder). Press **Enter** to accept the default in brackets, or type a path. See [section 5](#5--choosing-where-everything-lives) to skip this question. |
| `Welcome to WRFall. You are installing WRF/WPS.` | A generic banner. With `list` nothing is installed or changed. |
| `Installed release : 4.7.0` | Read from `<home>/strc/.release`. It says `No WRF installed` on a fresh machine. |
| `<- Most current` | The newest release found on GitHub. This is what `--install` and `--update` pick when you give no version. |
| `<- Currently installed` | The release recorded in your home directory. |

### Good to know

- The list is fetched **live** from the WRF GitHub repository, so yours will differ as new releases appear.
- If GitHub cannot be reached (no network, or rate limiting), WRFall falls back to its built-in default, **4.6.0**, and lists only that.
- Want datasets and packages listed too? Use `--install listall`, or `--install listgeog` for terrestrial datasets only.

```bash
./wrfall_install.py --install listall --homedir ~/Build_WRF
```

---

## 5. 🏠 Choosing where everything lives

WRFall keeps everything under one **home directory**. It decides where in this order:

1. `--homedir DIR` on the command line
2. The `WRF_HOME` environment variable
3. The home recorded in the current domain (when you run `./wrfall` from inside a domain)
4. The default `~/Build_WRF`, after asking you interactively (skipped with `--allyes`)

To avoid the question every time:

```bash
# one-off
./wrfall_install.py --install list --homedir ~/Build_WRF

# permanent (add to ~/.bashrc)
export WRF_HOME=$HOME/Build_WRF
```

### What gets created there

```text
~/Build_WRF/
├── LIBRARIES/            library source trees
├── local/                installed libraries (NETCDF, HDF5, MPICH, flex, ...)
├── packages/             downloaded tarballs (kept unless --scrubpkgs)
├── logs/                 one log file per library and per WRF/WPS build
├── etc/modulefiles/      WRFARW, WRFARW.sh, WRFARW.csh, ... environment files
├── strc/.release         record of installed release and build types
├── WPS_GEOG/             terrestrial data
├── Scripts/              shared utility scripts + WRF-domain-Wizard
├── WRF-4.7.0-ARW/        WRF builds, named WRF-<version>-<Type>
├── WPS-4.7.0-ARW/        WPS builds, named WPS-<version>-<Type>
└── ethiowrf/             a run domain (see section 10)
```

---

## 6. 🔧 Installing WRF and WPS

### The simplest case

```bash
./wrfall_install.py --install -arw
```

This installs the **newest release** for the ARW build type. Pick a specific release by naming it:

```bash
./wrfall_install.py --install 4.6.0 -arw
```

### Build types

| Flag | Build | Typical use |
|:---:|---|---|
| `-arw` | WRF-ARW | Standard weather forecasting |
| `-chem` | WRF-Chem | Atmospheric chemistry and air quality |
| `-hydro` | WRF-Hydro | Coupled hydrology |
| `-fire` | WRF-Fire (real data) | Wildfire spread with real terrain and fuels |
| `-fireideal` | WRF-Fire (ideal) | `em_fire` idealised test cases (WRF only, no WPS) |
| `-all` | arw + chem + hydro + fire | Everything except `-fireideal` |

You can combine them: `--install -arw -chem`. The long form `--type arw` also works and can be repeated.

### Rehearse first with a dry run

```bash
./wrfall_install.py --install -arw --dryrun
```

This prints what would be downloaded and built, without touching anything.

### What happens during an install

1. 🔍 **Checks** for required compilers and tools, free disk space, and your CPU layout.
2. ❓ **Asks for confirmation** (skipped with `--allyes`).
3. 📚 **Builds nine support libraries** into `<home>/local`. Libraries that already exist are skipped.
4. 🌀 **Downloads and compiles WRF** for each build type (about 20–40+ minutes each).
5. 🧭 **Downloads and compiles WPS** (geogrid, ungrib, metgrid) and sets `geog_data_path` in its `namelist.wps`.
6. 🌐 **Downloads the base WPS_GEOG** terrestrial data (unless `--nogeog`).
7. 📝 **Writes environment files** and `~/WRF-DoList.txt`, and records the release in `<home>/strc/.release`.
8. 🧙 **Fetches WRF-domain-Wizard** into `<home>/Scripts`.

Every compile writes a log under `<home>/logs/`, for example `wrf_arw_4.7.0.log`.

### Useful install flags

| Flag | Effect |
|---|---|
| `--allyes` | Answer yes to everything, for unattended runs |
| `--continue` | Resume an interrupted install, skipping finished libraries |
| `--force` | Rebuild WRF/WPS even if present; also allows installing an *earlier* release than the one you have |
| `--force-libs` | Rebuild the support libraries as well |
| `--nogeog` | Skip the big WPS_GEOG download |
| `--nolocal` | Ignore cached tarballs in `<home>/packages` |
| `--nounpack` | Download only; do not unpack or build |
| `--scrubpkgs` | Delete tarballs after installation |
| `--scour` | Remove the previous release after a successful install |
| `--jobs N` | `make -j N` (default: up to 8, or your core count) |
| `--wrf-opt N` | WRF configure menu choice (default `34`: gfortran, dmpar) |
| `--wps-opt N` | WPS configure menu choice (default `3`: gfortran, dmpar) |
| `--nest-opt N` | Nesting option (default `1`, basic) |

### Interrupted? Resume.

```bash
./wrfall_install.py --install -arw --continue
```

Partial downloads are resumed automatically, even after Ctrl-C or a dropped connection.

### Pick a release carefully

- Newest is not always smoothest. **WRF-Chem 4.7 and later** has a known KPP build problem (see [section 17](#17--wrf-chem)). For chemistry, `4.6.0` is the safe choice.
- WRFall refuses to install a release **older** than the one already installed unless you pass `--force`.
- If the requested release is already fully built, WRFall says so and exits. Use `--force` to rebuild.

---

## 7. 🌱 Loading the environment

After an install, read **`~/WRF-DoList.txt`**. It contains instructions tailored to your machine. In short:

### With Environment Modules (if `modulecmd` exists)

```bash
# add to ~/.bashrc
export MODULEPATH="${MODULEPATH}:$HOME/Build_WRF/etc/modulefiles"
module load WRFARW
```

### Without modules: source a file

```bash
source ~/Build_WRF/etc/modulefiles/WRFARW.sh      # bash
source ~/Build_WRF/etc/modulefiles/WRFARW.csh     # tcsh
```

The files are named after the build type: `WRFARW`, `WRFChem`, `WRFHydro`, `WRFFire`, `WRFFireIdeal`.

> ⚠️ Load **one build type at a time**. Also comment out old WRF environments in your login files; they can conflict.

These files set `WRF_HOME`, `WRF_DIR`, `WPS_DIR`, `WPS_GEOG`, `NETCDF`, `HDF5`, the JasPer paths, compiler variables, and `PATH` / `LD_LIBRARY_PATH` for the local libraries. Chem and Hydro files also set `WRF_CHEM`, `WRF_KPP`, `WRF_HYDRO` and friends.

---

## 8. 🏔️ Terrestrial (geography) data

WPS needs static fields such as terrain, land use and soil type. WRFall downloads the standard **`geog_high_res_mandatory`** set into `<home>/WPS_GEOG` during install.

```bash
# Skip it during install (maybe you already have a copy)
./wrfall_install.py --install -arw --nogeog

# Add datasets later
./wrfall_install.py --addgeog chem          # all WRF-Chem datasets
./wrfall_install.py --addgeog mandatory     # the base set
./wrfall_install.py --addgeog erod,albedo_ncep   # individual UCAR datasets
./wrfall_install.py --addgeog fire          # WRF-Fire data folders
```

### Chem dataset group

`--addgeog chem` fetches: `modis_landuse_21class_30s`, `soiltype_top_2m`, `soiltype_bot_2m`, `albedo_ncep`, `maxsnowalb`, `erod`, `clayfrac_5m`, `sandfrac_5m`.

### Using an existing WPS_GEOG

Place or link it at `<home>/WPS_GEOG` and install with `--nogeog`. Each domain's `namelist.wps` has `geog_data_path` set to this folder.

> 📦 Any other UCAR dataset name is tried as `<name>.tar.bz2` from the UCAR WPS files site, with a warning that it is not in the built-in list.

---

## 9. 🎁 Optional packages

```bash
./wrfall_install.py --addpack domwiz,scripts,cdsapi
```

| Package | Aliases | What it gives you |
|---|---|---|
| `domwiz` | `dwiz`, `wizard`, `domainwizard` | **WRF-domain-Wizard**, a graphical domain designer, cloned into `<home>/Scripts` |
| `scripts` | `utils`, `script` | Your automation scripts (`setup_wrf_domain.sh`, `run_wrf.sh`, the GFS/ECMWF/ERA5 download scripts) saved in `<home>/Scripts` |
| `cdsapi` | `cds`, `era5` | The Python `cdsapi` module (pip `--user`), required for ERA5 |

`--addpack` and `--addgeog` are stand-alone options. They cannot be combined with `--update`, and they need an existing installation (or `--homedir` pointing to one).

### ERA5 credentials

ERA5 downloads also need a Copernicus Climate Data Store account and a `~/.cdsapirc` file with your API key. WRFall reminds you when the file is missing.

---

## 10. 🗺️ Creating a domain

A **domain** is a self-contained run directory with its own namelists, executables (as links) and data folders.

```bash
./wrfall_install.py --mkdomain ethiowrf
```

If you have more than one build installed (say ARW and Chem), tell WRFall which one the domain is for:

```bash
./wrfall_install.py --mkdomain smogdom -chem
./wrfall_install.py --mkdomain firedom -fire
```

Without a type flag and with several builds present, WRFall lists them and stops, so you never get the wrong one by accident. (`-fireideal` has no WPS and cannot be used for domains.)

### What you get

```text
~/Build_WRF/ethiowrf/
├── GFS_Input/            ┐
├── ECMWF_Input/          ├ each holds YYYYMMDDCC/ sub-folders of GRIB data
├── ERA5_Input/           ┘
├── wrf_output/
│   ├── metgrid/          met_em files, per cycle
│   ├── ungrib/           ungrib intermediates and logs, per cycle
│   ├── geogrid/          geo_em files
│   └── Plots/            for your own plots
├── namelist.wps          editable copy
├── namelist.input        editable copy
├── domain_env.sh         source it to get WRF_HOME, WRF_WPS, WRF_RUN, DOMAIN_DIR
├── wrfall -> .../wrfall_install.py
└── (links to *.exe, link_grib.csh, geogrid/ ungrib/ metgrid/ tables, run/ files)
```

Domain names may contain letters, digits, dot, dash and underscore.

### Edit `namelist.wps` first

Open `namelist.wps` (or use **WRF-domain-Wizard** from `<home>/Scripts`) and set your area: `ref_lat`, `ref_lon`, `e_we`, `e_sn`, `dx`, `dy`, `map_proj` and so on. WRFall reads this file to decide which area to download.

### Work from inside the domain

```bash
cd ~/Build_WRF/ethiowrf
./wrfall --getdata gfs --date 20261016 --cycle 00 --end 72
./wrfall --run auto gfs
```

Run from inside a domain, `--domain` is not needed and `--homedir` is picked up automatically.

### Shared scripts

`<home>/Scripts` is created once and shared by all domains. It holds your utility `*.sh` / `*.py` scripts and a clone of WRF-domain-Wizard.

---

## 11. 📡 Downloading forecast data

WRFall can fetch three sources into `<domain>/<Source>_Input/YYYYMMDDCC/`.

| Source | Command | Resolution | Notes |
|---|---|:---:|---|
| 🇺🇸 **GFS** | `--getdata gfs` | 0.25° | From NOAA NOMADS; subset to your area |
| 🇪🇺 **ECMWF IFS** | `--getdata ecmwf` | 0.25° | Open data from `data.ecmwf.int` |
| 🛰️ **ERA5** | `--getdata era5` | 0.25° | Reanalysis through the CDS API (needs `cdsapi`) |

### Examples

```bash
# 72 hours of GFS, 00 UTC cycle, every 3 hours
./wrfall_install.py --getdata gfs --domain ~/Build_WRF/ethiowrf --date 20261016 --cycle 00 --end 72

# ECMWF, hours 0 to 48, every 6 hours
./wrfall_install.py --getdata ecmwf --domain ethiowrf --date 20261016 --end 48 --step 6

# ERA5, one day
./wrfall_install.py --getdata era5 --domain ethiowrf --date 20261016 --end 21

# Two domains at once
./wrfall_install.py --getdata gfs --domain dom1,dom2 --date 20261016 --end 72
```

### Options

| Option | Default | Meaning |
|---|:---:|---|
| `--date YYYYMMDD` | today (UTC) | Forecast start date |
| `--cycle 00\|06\|12\|18` | `00` | Model cycle, UTC |
| `--start N` | `0` | First forecast hour |
| `--end N` | `120` (GFS/ECMWF), `21` (ERA5) | Last forecast hour |
| `--step N` | `3` | Hours between files |
| `--margin DEG` | `1.0` | Degrees added around the domain extent |
| `--bbox W E S N` | from domain | Explicit area, overriding `namelist.wps` |
| `--outdir DIR` | automatic | Put the files somewhere else |
| `--domain A[,B,...]` | current dir if it is a domain | Domain name(s) or path(s) |

### How the area is chosen

WRFall computes the lat/lon extent of **domain 1** from `namelist.wps` using the same map projections as WPS (Lambert, polar, Mercator, lat-lon), adds `--margin` degrees, and snaps the result outward to the 0.25° grid. It prints the extent and the download area so you can check them. Without a `namelist.wps` it falls back to a default box (lat -10 to 25, lon 20 to 54) and warns you.

### Behaviour worth knowing

- 🔁 Existing non-empty files are **skipped**, so you can rerun a command to fill gaps. `--force` downloads them again.
- ⏳ A `[FAIL]` line usually means the cycle is **not yet published** or the request was rejected. Try again later. The summary shows the number of failed files.
- 🐌 GRIB downloads use at most **3** parallel connections to stay polite to the servers, whatever `--dl-jobs` says.
- 🧪 `--dryrun` lists what would be fetched.
- 📅 For ERA5, requests are made per calendar day, one for surface and one for pressure levels.

---

## 12. ▶️ Running WRF

`--run` executes the pipeline for a domain: **(geogrid) → ungrib → metgrid → real → wrf**.

### Syntax

```bash
# automatic: today (UTC), cycle 00, 72 hours, GFS
./wrfall_install.py --run auto --domain ethiowrf

# choose the source
./wrfall_install.py --run auto ecmwf --domain ethiowrf

# explicit date, cycle, hours (1 to 72), optional source
./wrfall_install.py --run 20261016 00 72 --domain ethiowrf
./wrfall_install.py --run 20261016 12 48 era5 --domain ethiowrf

# several domains one after another
./wrfall_install.py --run auto --domain dom1,dom2
```

If you omit the source, `--source` is used (default `gfs`).

### What each run does

| Step | Detail |
|---|---|
| 1. Back up | Copies `namelist.wps` and `namelist.input` to `*.bak.<timestamp>` |
| 2. Set dates | Writes `start_date` / `end_date` in `namelist.wps` |
| 3. geogrid | Only when `RUN_GEOGRID=1` |
| 4. Link GRIB | Runs `link_grib.csh` on the files in `<domain>/<Source>_Input/YYYYMMDDCC/` and sets the right `Vtable` |
| 5. ungrib | `Vtable.GFS`, `Vtable.ECMWF`, or `Vtable.ERA-interim.pl` |
| 6. metgrid | Copies `met_em*` into `wrf_output/metgrid/<cycle>/` and archives ungrib products |
| 7. namelist.input | Sets `run_hours` and the start/end year, month, day, hour |
| 8. real.exe | With `REAL_NP` MPI ranks |
| 9. wrf.exe | With `WRF_NP` MPI ranks |
| 10. Collect | Moves `wrfout_*` into `<domain>/wrf_output/<YYYYMMDDCC>/` |

A summary line gives the output folder and elapsed time. The `log.*` files stay in the domain for inspection.

### Environment variables

| Variable | Default | Meaning |
|---|:---:|---|
| `WRF_NP` | `8` | MPI ranks for `wrf.exe`. Match this to your physical cores. |
| `REAL_NP` | `2` | MPI ranks for `real.exe` |
| `RUN_GEOGRID` | unset | `1` runs `geogrid.exe` and archives `geo_em*` |
| `ERA5_VTABLE_PL` / `ERA5_VTABLE_SFC` | `Vtable.ERA-interim.pl` | Override the ERA5 Vtables |

```bash
WRF_NP=16 RUN_GEOGRID=1 ./wrfall --run auto gfs
```

### ERA5 runs

ERA5 is ungribbed in two passes, pressure levels (prefix `PRES`) and surface (prefix `SFC`), and `fg_name` is set to match. WRFall does this for you.

### Before you run

- The domain needs `ungrib.exe`, `metgrid.exe`, `real.exe`, `wrf.exe` and `link_grib.csh`. A missing file stops the run with a clear message.
- The GRIB folder must exist. If it does not, WRFall tells you the exact `--getdata` command to run.
- Run the **same build type** you made the domain for, with its environment loaded (see [section 7](#7--loading-the-environment)).
- The forecast length is limited to **1 to 72 hours** per `--run`.

---

## 13. 🔄 Updating to a newer release

```bash
./wrfall_install.py --update            # newest release
./wrfall_install.py --update 4.8.0      # a specific one
./wrfall_install.py --update list       # just list releases
```

What happens:

- The new release is built **next to** the old one, for the build types you already have (or those you name with flags).
- Libraries are reused, so an update is much faster than a fresh install.
- New environment files are written and `<home>/strc/.release` is updated.
- WRFall-created domains are **refreshed**: executables are re-linked to the new build and `geog_data_path` is rechecked. Your namelists are kept, with backups saved as `*.bak`.

| Flag | Effect |
|---|---|
| `--norefresh` | Leave existing domains alone |
| `--scour` | Delete the old release directories afterwards |
| `--force` | Update even if you are current |

If you are already on the newest release, WRFall says: *"No need to update - You are current."*

> 💡 With your example (4.7.0 installed, 4.8.0 available) you would run `./wrfall_install.py --update`. For a Chem build, think about whether the newer release has the known KPP issue before updating.

---

## 14. 📦 Importing domains from an old installation

Bring in domains (any folder containing `namelist.wps` and `namelist.input`) from a previous setup:

```bash
./wrfall_install.py --install -arw --import ~/old_wrf/domains
```

Each domain is copied with `rsync`, re-linked to the current build, and its `geog_data_path` is updated. A domain whose name already exists is skipped with a warning. `--import` may be given several times.

---

## 15. 🌐 Network, proxies and offline installs

### Downloaders

By default WRFall tries `wget`, then `curl`, then its own built-in downloader. Force one with `--wget` or `--curl`.

The built-in downloader is fast too: it opens several **parallel range connections** per file and resumes each segment after a failure.

```bash
./wrfall_install.py --install -arw --dl-jobs 12     # more parallelism (max 16)
./wrfall_install.py --install -arw --dl-jobs 1      # single stream
```

### Proxies

Proxy variables are **cleared** by default. To use a proxy:

```bash
./wrfall_install.py --install -arw --proxy proxy.example.org:3128
```

### Offline or repeated installs

Put all tarballs in a local folder and point WRFall at it:

```bash
./wrfall_install.py --install -arw --repodir /data/wrf_repo
```

WRFall looks for each file by name in the folder (or its sub-folders) and copies it instead of downloading. A missing file stops the install with a message.

Downloaded tarballs are cached in `<home>/packages`, so reruns are quick. Use `--nolocal` to force fresh copies.

---

## 16. 🔥 WRF-Fire

WRFall supports two fire builds.

| Build | Command | Use |
|---|---|---|
| **Real data** | `--install -fire` | Standard WRF-ARW source compiled with `./compile em_real`, plus WPS prepared for the fire grid |
| **Ideal cases** | `--install -fireideal` | Compiled with `./compile em_fire`; WRF only |

No special environment variable is needed: the fire module is part of every WRF-ARW build and is switched on in `namelist.input`.

### Real-data workflow

1. **Install:** `./wrfall_install.py --install -fire`
2. **Prepare data folders:** `./wrfall_install.py --addgeog fire` (also done during `--install -fire`). This creates `WPS_GEOG/landfire/` and `WPS_GEOG/highres_elev/` plus a README.
3. **Supply two datasets** that UCAR does not provide, converted to geogrid binary format:
   - **NFUEL_CAT**: Anderson 13 fuel categories (for example USGS LANDFIRE) into `landfire/`
   - **ZSF**: high-resolution terrain height (1/3 or 1 arc-second DEM) into `highres_elev/`
4. **Create a domain:** `./wrfall_install.py --mkdomain firedom -fire`
5. **Edit** `&fire` in `namelist.input` (ignition points, times, radius) and keep grid settings consistent with `namelist.wps`.
6. **Run** as usual.

What the `-fire` build and domain set up for you:

- A `GEOGRID.TBL.ARW_FIRE` with `NFUEL_CAT` and `ZSF` (both `subgrid=yes`), linked as `GEOGRID.TBL`
- `subgrid_ratio_x/y = 10` in `namelist.wps`
- `sr_x` / `sr_y = 10` in `&domains` and a ready `&fire` block (`ifire=2`, `fire_fuel_read=-1`) in `namelist.input`
- An editable `namelist.fire` copy in the domain

> ⚠️ The fire datasets use `halt_on_missing=yes`, so geogrid stops if either is missing for your area.

### Ideal cases

```bash
cd ~/Build_WRF/WRF-4.7.0-FireIdeal/test/em_fire
ln -sf namelist.input_two_fires namelist.input
ln -sf input_sounding_two_fires input_sounding
./ideal.exe
./wrf.exe          # the default case is hill_simple
```

---

## 17. 🧪 WRF-Chem

```bash
./wrfall_install.py --install 4.6.0 -chem
./wrfall_install.py --addgeog chem
```

### What WRFall handles for you

| Problem | What WRFall does |
|---|---|
| KPP needs byacc and flex built from source | Builds both into `<home>/local` and checks them before compiling |
| Broken Debian/Ubuntu `flex` package | Never used; a functional flex/yacc smoke test runs first |
| `configure.wrf` overriding exported tools | Re-points `YACC` / `FLEX` to the local ones after `./configure` |
| WRF-Chem 4.7.x tuv_kpp path bug in `compile_wkc` | Patches the path automatically |
| GCC 10+ rejecting mixed-type arguments (WRF-Chem 4.7+) | Adds `-fallow-argument-mismatch` to the build and env files |

### Known issue with WRF 4.7 and later

WRF ≥ 4.7 has a KPP build problem on recent GCC (see [wrf-model/wrf#2259](https://github.com/wrf-model/wrf/issues/2259)), typically shown as `third body ... M` plus one `Error 127` per mechanism. WRFall works around most cases. If the build still fails, install 4.6.0:

```bash
./wrfall_install.py --install 4.6.0 -chem --force
```

### If the compile fails

WRFall prints the **first errors** from the log with line numbers (the first is the cause, the rest is fallout) and, for known KPP signatures, a specific hint. Useful steps:

```bash
conda deactivate 2>/dev/null          # conda's flex/bison can shadow the local ones
./wrfall_install.py --install -chem --force-libs --force
```

Run in the same shell where you loaded `WRFChem`.

---

## 18. 💧 WRF-Hydro

```bash
./wrfall_install.py --install -hydro
```

WRFall downloads NCAR's `wrf_hydro_nwm_public` **v5.3.0**, merges its `NDHMS` sources into WRF's `hydro/` folder, and builds with `WRF_HYDRO=1`. Use the `WRFHydro` environment file when running.

---

## 19. 📚 Complete flag reference

### Primary actions

| Flag | Description |
|---|---|
| `--install [REL\|list\|listall\|listgeog]` | Fresh install; `REL` like `4.6.0`, omitted means newest |
| `--update [REL\|list]` | Build a newer release, reuse libraries, refresh domains |
| `--addpack LIST` | Optional packages: `domwiz`, `scripts`, `cdsapi` |
| `--addgeog LIST` | Terrestrial datasets: `chem`, `mandatory`, `fire`, or UCAR names |
| `--mkdomain NAME` | Create a run domain |
| `--getdata gfs\|ecmwf\|era5` | Download input data |
| `--run auto\|YYYYMMDD CC HH [src]` | Run the WPS + WRF chain |

`--install` and `--update` are mutually exclusive.

### Build types

`-arw` `-chem` `-hydro` `-fire` `-fireideal` `-all` or `--type NAME`

### Installation options

| Flag | Description |
|---|---|
| `--homedir DIR` | Top-level folder (default `$WRF_HOME` or `~/Build_WRF`) |
| `--repodir DIR` | Take tarballs from a local repository |
| `--import DIR` | Import domains from an older installation (repeatable) |
| `--allyes` | Answer yes to all questions |
| `--continue` | Resume, skipping finished libraries |
| `--force` | Rebuild WRF/WPS; allow older release |
| `--force-libs` | Also rebuild the support libraries |
| `--nolocal` | Ignore cached tarballs |
| `--nounpack` | Download only |
| `--scrubpkgs` | Delete tarballs after install |
| `--norefresh` | Do not refresh domains on update |
| `--nogeog` | Skip WPS_GEOG download |
| `--scour` | Remove the previous release afterwards |
| `--wrf-opt N`, `--nest-opt N`, `--wps-opt N` | Override configure menu choices |
| `--jobs N` | Parallel `make` jobs |

### Network options

| Flag | Description |
|---|---|
| `--curl`, `--wget` | Force a downloader |
| `--dl-jobs N` | Parallel connections per download (1 to 16, default 8) |
| `--proxy HOST:PORT` | Use an HTTP(S) proxy |

### Data and run options

| Flag | Description |
|---|---|
| `--domain A[,B,...]` | Domain name(s) or path(s) |
| `--date YYYYMMDD` | Forecast date |
| `--cycle CC` | `00`, `06`, `12`, `18` |
| `--start N`, `--end N`, `--step N` | Forecast hour range and interval |
| `--bbox W E S N` | Explicit download area |
| `--margin DEG` | Margin around the domain extent |
| `--outdir DIR` | Download location override |
| `--source gfs\|ecmwf\|era5` | Default source for `--run` |

### Housekeeping

| Flag | Description |
|---|---|
| `--dryrun` | Show actions without doing them |
| `--debug` | Print each command that is run |
| `--version` | Show the version |
| `--flags` | Show the flag list |
| `-h`, `--help` | Show the short help |

---

## 20. 🛟 Troubleshooting and FAQ

### Installation

**"Missing required build tools"**
Install the compilers and tools listed in [section 2](#2--before-you-begin), or ask your administrator.

**The install stopped halfway.**
Rerun the same command with `--continue`. Finished libraries are skipped and partial downloads resume.

**A build failed. Where do I look?**
In `<home>/logs/`, for example `wrf_arw_4.7.0.log`. WRFall also prints the first errors and a context command like `sed -n '120,140p' <log>`.

**"Installation Faux Paux"**
You asked for a release older than the installed one. Add `--force` if you really mean it.

**Everything says "already installed".**
That is the skip logic. Use `--force` to rebuild WRF/WPS, or `--force-libs --force` to rebuild everything.

**How do I see what would happen before committing?**
`--dryrun`, optionally with `--debug`.

### Downloads

**Downloads are slow or fail.**
Try `--wget` or `--curl`, change `--dl-jobs`, or set `--proxy` if you are behind one. Rerunning resumes where it stopped.

**`[FAIL]` on GFS or ECMWF files.**
The cycle may not be published yet (GFS takes a few hours after the cycle time), or the date is too old for the server. Retry later or pick an earlier cycle.

**ERA5 says cdsapi is missing.**
Run `./wrfall_install.py --addpack cdsapi` and create `~/.cdsapirc` with your CDS key.

**`--install list` shows only one release.**
GitHub could not be reached (or rate-limited you), so the built-in default 4.6.0 is shown. Try again later.

### Domains and runs

**"More than one build is installed"**
Add the type flag: `--mkdomain NAME -arw` (or `-chem`, `-hydro`, `-fire`).

**"Required file not found in the domain"**
The domain's links are missing. Recreate or refresh it with `--update`, or run `--mkdomain` again for the same name.

**"GRIB input directory not found"**
Download first with the `--getdata` command printed in the message. The folder name must be `<Source>_Input/YYYYMMDDCC`.

**`--run` refuses my forecast length.**
`--run` accepts 1 to 72 hours per call.

**WRF is slow.**
Set `WRF_NP` to your number of **physical** cores and avoid oversubscribing hyper-threads.

**The geogrid step is skipped.**
By design. Set `RUN_GEOGRID=1` for the first run, or whenever you change the domain.

### Tips

- 🧭 Press **Ctrl-C** any time; WRFall exits politely and downloads can be resumed.
- 🧹 Add `--scrubpkgs` to save space once you are happy, or leave tarballs in `packages/` for fast rebuilds.
- 🗂️ Keep one domain per project and use `--domain a,b,c` to batch them.

---

## 21. 🧾 Command cheat sheet

```bash
# 1. Get it
git clone https://github.com/goikelem/WRFall.git && cd WRFall && chmod +x wrfall_install.py

# 2. Look around
./wrfall_install.py --install list
./wrfall_install.py --flags

# 3. Install
./wrfall_install.py --install -arw                 # newest release
./wrfall_install.py --install 4.6.0 -chem          # specific release
./wrfall_install.py --install -all --allyes        # everything, unattended

# 4. Extras
./wrfall_install.py --addpack domwiz,scripts,cdsapi
./wrfall_install.py --addgeog chem

# 5. Environment
source ~/Build_WRF/etc/modulefiles/WRFARW.sh

# 6. Domain
./wrfall_install.py --mkdomain mydom
#    edit ~/Build_WRF/mydom/namelist.wps

# 7. Data
cd ~/Build_WRF/mydom
./wrfall --getdata gfs --date 20261016 --cycle 00 --end 72

# 8. Run
RUN_GEOGRID=1 WRF_NP=8 ./wrfall --run 20261016 00 72 gfs

# 9. Update later
./wrfall_install.py --update
```

---

<p align="center">
  Found a bug or have an idea? Open an issue at
  <a href="https://github.com/goikelem/WRFall">github.com/goikelem/WRFall</a>.<br>
  <i>Now go forth and simulate!</i> 🌪️
</p>
