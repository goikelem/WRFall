<h1 align="center">🌪️ WRFall 🌦️</h1>

<p align="center">
  <b>The no-root installer, updater and daily-driver for WRF + WPS</b><br>
  <i>ARW · Chem · Hydro · Fire — from bare Linux to a running forecast in one script.</i>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.4.2-ff6b6b?style=for-the-badge" alt="version">
  <img src="https://img.shields.io/badge/python-3.6%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="python">
  <img src="https://img.shields.io/badge/dependencies-none-2ecc71?style=for-the-badge" alt="stdlib only">
  <img src="https://img.shields.io/badge/root-not%20needed-f39c12?style=for-the-badge" alt="no root">
  <img src="https://img.shields.io/badge/platform-Linux%20x86__64-9b59b6?style=for-the-badge&logo=linux&logoColor=white" alt="linux">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/WRF-ARW-1abc9c?style=flat-square" alt="ARW">
  <img src="https://img.shields.io/badge/WRF-Chem-e67e22?style=flat-square" alt="Chem">
  <img src="https://img.shields.io/badge/WRF-Hydro-3498db?style=flat-square" alt="Hydro">
  <img src="https://img.shields.io/badge/WRF-Fire-e74c3c?style=flat-square" alt="Fire">
  <img src="https://img.shields.io/badge/data-GFS%20%7C%20ECMWF%20%7C%20ERA5-8e44ad?style=flat-square" alt="data">
</p>

---

## ✨ Why WRFall?

Building WRF by hand means a dozen libraries, compiler quirks and a lot of waiting. **WRFall** wraps the whole workflow into a single, dependency-free Python script:

| | Feature | What you get |
|---|---|---|
| 🔧 | **No-root install** | zlib, HDF5, NetCDF-C/Fortran, MPICH, libpng, JasPer, flex and byacc are built into `~/Build_WRF/local` |
| 🧩 | **Four build types** | `-arw`, `-chem`, `-hydro`, `-fire` (plus `-fireideal` for `em_fire` ideal cases) |
| 🔄 | **Painless updates** | Build a newer release next to the old one, reuse the libraries, refresh your domains |
| 🗺️ | **Domain creation** | One command makes a ready-to-run domain with the right links and folders |
| 📡 | **Data download** | GFS 0.25°, ECMWF IFS 0.25° and ERA5, cropped to *your* domain |
| ⚡ | **Fast transfers** | Parallel HTTP range downloads with per-segment resume and live MB/s |
| ▶️ | **One-line runs** | geogrid → ungrib → metgrid → real → wrf, with outputs archived |
| 🛡️ | **Smart safeguards** | KPP/flex/yacc checks and automatic fixes for WRF-Chem 4.7.x on modern GCC |

---

## 🚀 Quick start

```bash
# 1. Install WRF + WPS (pick one or more types)
./wrfall_install.py --install -arw

# 2. Create a domain
./wrfall_install.py --mkdomain ethiowrf

# 3. Edit namelist.wps for your area, then download data for it
./wrfall_install.py --getdata gfs --domain ~/Build_WRF/ethiowrf --date 20261016 --end 72

# 4. Run the whole chain
./wrfall_install.py --run 20261016 00 72 --domain ~/Build_WRF/ethiowrf
```

> 💡 Prefer working from inside the domain? Each domain gets a `wrfall` soft link:
> ```bash
> cd ~/Build_WRF/ethiowrf
> ./wrfall --getdata gfs --date 20261016 --cycle 00 --end 72
> ./wrfall --run auto gfs
> ```

---

## 🧰 Requirements

- 🐧 64-bit Linux
- 🐍 Python **3.6+** (standard library only)
- 🛠️ `gcc`, `g++`, `gfortran`, `make`, `tar`, `perl`
- ➕ Recommended: `tcsh`/`csh`, `m4`, `git`
- 🌍 `cdsapi` only for ERA5 (`--addpack cdsapi`), plus credentials in `~/.cdsapirc`

```bash
# Debian / Ubuntu
sudo apt install build-essential gfortran perl tcsh m4 git

# RHEL / Rocky
sudo dnf groupinstall 'Development Tools' && sudo dnf install gcc-gfortran perl tcsh m4 git
```

---

## 🎨 Build types

| Flag | Build | Notes |
|:---:|---|---|
| `-arw` | 🟢 WRF-ARW | Standard real-data build |
| `-chem` | 🟠 WRF-Chem | KPP enabled, local flex/byacc forced into the build |
| `-hydro` | 🔵 WRF-Hydro | NCAR `wrf_hydro_nwm_public` v5.3.0 merged in |
| `-fire` | 🔴 WRF-Fire | Real-data build, fire-ready WPS (`NFUEL_CAT`, `ZSF`, subgrid) |
| `-fireideal` | 🟥 WRF-Fire ideal | `./compile em_fire`, WRF only |
| `-all` | 🟢🟡🔴 All of the above | `arw`, `chem`, `hydro`, `fire` |

You can also use `--type arw|chem|hydro|fire|fireideal` (repeatable).

---

## 📋 Command cheat sheet

### 📦 Install and update
```bash
./wrfall_install.py --install list          # see available releases
./wrfall_install.py --install 4.6.0 -chem   # a specific release and type
./wrfall_install.py --update                # newer release, libraries reused
./wrfall_install.py --update --scour        # ...and remove the old release
```

### ➕ Extras
```bash
./wrfall_install.py --addpack domwiz,scripts,cdsapi
./wrfall_install.py --addgeog chem          # WRF-Chem terrestrial datasets
./wrfall_install.py --addgeog fire          # landfire/ + highres_elev/ folders
```

### 🗺️ Domains
```bash
./wrfall_install.py --mkdomain mydom
./wrfall_install.py --mkdomain firedom -fire   # adds &fire, sr_x/sr_y, subgrid_ratio
./wrfall_install.py --install -all --import ~/old_domains
```

### 📡 Data
```bash
./wrfall_install.py --getdata gfs   --domain mydom --date 20261016 --cycle 00 --end 72
./wrfall_install.py --getdata ecmwf --domain mydom --date 20261016 --end 72
./wrfall_install.py --getdata era5  --domain mydom --date 20261016 --end 24
./wrfall_install.py --getdata gfs --bbox 30 50 3 15      # W E S N, overrides namelist.wps
```

The download area is read from the **d01 lat/lon extent** in `namelist.wps` (all WPS projections supported), plus `--margin` degrees (default 1.0), snapped outward to 0.25°.

### ▶️ Run
```bash
./wrfall_install.py --run auto gfs --domain mydom
./wrfall_install.py --run 20261016 00 72 ecmwf --domain mydom
./wrfall_install.py --run auto --domain dom1,dom2,dom3   # several domains
```

| Variable | Default | Meaning |
|---|:---:|---|
| `WRF_NP` | `8` | MPI ranks for `wrf.exe` |
| `REAL_NP` | `2` | MPI ranks for `real.exe` |
| `RUN_GEOGRID` | unset | Set to `1` to run `geogrid.exe` first |

---

## 🟢🟡🔴 All options

| Flag | Purpose |
|---|---|
| `--homedir DIR` | Install location (default `$WRF_HOME` or `~/Build_WRF`) |
| `--repodir DIR` | Use tarballs from a local repository |
| `--continue` | Resume, skipping libraries already built |
| `--force` | Rebuild WRF/WPS (libraries are kept) |
| `--force-libs` | Rebuild the support libraries too |
| `--nogeog` | Skip the base WPS_GEOG download |
| `--nolocal` | Ignore cached tarballs |
| `--nounpack` | Download only |
| `--scrubpkgs` | Delete tarballs after install |
| `--norefresh` | Don't refresh domains on `--update` |
| `--curl` / `--wget` | Force a downloader (default: wget, curl, then built-in) |
| `--dl-jobs N` | Parallel connections per file (default 8, `1` = single stream) |
| `--proxy HOST:PORT` | Use a proxy |
| `--wrf-opt N` / `--wps-opt N` | Configure menu choices (defaults `34` and `3`: gfortran dmpar) |
| `--jobs N` | `make -j` parallelism |
| `--allyes` | Answer yes to every prompt |
| `--dryrun` | Show what would happen |
| `--debug` · `--version` · `--flags` · `--help` | Housekeeping |

---

## 📁 Layout

```text
~/Build_WRF/
├── LIBRARIES/            # library sources
├── local/                # installed libraries (NETCDF, HDF5, MPICH, ...)
├── packages/             # downloaded tarballs
├── logs/                 # one log per library / build
├── etc/modulefiles/      # WRFARW, WRFARW.sh, WRFARW.csh, ...
├── WPS_GEOG/             # terrestrial data
├── Scripts/              # shared utility scripts + WRF-domain-Wizard
├── WRF-4.6.0-ARW/        # WRF builds
├── WPS-4.6.0-ARW/        # WPS builds
└── ethiowrf/             # a domain
    ├── GFS_Input/YYYYMMDDCC/
    ├── ECMWF_Input/YYYYMMDDCC/
    ├── ERA5_Input/YYYYMMDDCC/
    ├── wrf_output/{metgrid,ungrib,Plots,geogrid}/
    ├── namelist.wps  namelist.input
    ├── domain_env.sh
    └── wrfall -> wrfall_install.py
```

After an install, a `~/WRF-DoList.txt` explains how to load the environment, either with Environment Modules or by sourcing the generated `.sh` / `.csh` file. Load **one** build type at a time.

---

## 🔥 WRF-Fire notes

- `-fire` needs two datasets that UCAR does not provide: `NFUEL_CAT` (Anderson 13 fuel categories) in `WPS_GEOG/landfire/` and `ZSF` (high-resolution terrain) in `WPS_GEOG/highres_elev/`.
- `--mkdomain NAME -fire` adds `sr_x`/`sr_y = 10`, a `&fire` block and `subgrid_ratio_x/y` for you.
- `-fireideal` cases run from `test/em_fire` (`ideal.exe`, then `wrf.exe`).

## 🧪 WRF-Chem notes

- Builds `flex` and `byacc` from source; the Debian/Ubuntu apt `flex` is known to break KPP.
- Runs a flex/yacc smoke test before compiling and points `configure.wrf` at the local tools.
- WRF-Chem ≥ 4.7 on GCC ≥ 10 gets `-fallow-argument-mismatch`, and the `compile_wkc` path bug is patched automatically.
- WRF ≥ 4.7 has a known KPP issue ([wrf-model/wrf#2259](https://github.com/wrf-model/wrf/issues/2259)); if it still fails, use `--install 4.6.0 -chem`.

---

## 🆘 Troubleshooting

| Symptom | Try |
|---|---|
| Download keeps failing | `--curl`, `--wget`, or lower `--dl-jobs` |
| Interrupted download | Just rerun it, segments resume |
| Build fails | Read the log in `~/Build_WRF/logs/`; the **first** error is the cause |
| Library seems stale | `--force-libs --force` |
| Check what will happen | `--dryrun --debug` |

---

## 🧬 Changelog highlights

- **1.4.2** WRF-Chem: local flex/yacc forced into `configure.wrf`, smoke tests, richer KPP hints
- **1.4.0** Shared `Scripts/` folder, `wrf_output/` layout, domain extent read from `namelist.wps`
- **1.3.0** Per-source input folders, `--domain a,b,c`, `wrfall` soft link
- **1.2.0** Parallel, resumable, streamed downloads

---

<p align="center">
  Made with ☕, 🌩️ and a lot of <code>make -j</code><br>
  <i>Now go forth and simulate!</i> 🌍
</p>
