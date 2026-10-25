<h1 align="center">🌪️ WRFall Cheat Sheet 🌦️</h1>

<p align="center">
  <b>No-root Python installer & runner for WRF/WPS</b> · ARW · Chem · Hydro · Fire<br>
  <img src="https://img.shields.io/badge/version-1.4.2-ff6b6b?style=flat-square" alt="version">
  <img src="https://img.shields.io/badge/python-3.6%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="python">
  <img src="https://img.shields.io/badge/dependencies-none-2ecc71?style=flat-square" alt="stdlib only">
  <img src="https://img.shields.io/badge/root-not%20needed-f39c12?style=flat-square" alt="no root">
  <img src="https://img.shields.io/badge/Linux-x86__64-9b59b6?style=flat-square&logo=linux&logoColor=white" alt="linux">
</p>

## 📥 Get it
```bash
git clone https://github.com/goikelem/WRFall.git && cd WRFall && chmod +x wrfall_install.py
```
## If you want to use Perl 
```bash
git clone https://github.com/goikelem/WRFall.git && cd WRFall && chmod +x wrfall_install.pl
```
# 🚩 Handy flags   if you want to use Perl; the only thing you change is the .py to .pl extension 
## 🔧 Install & update
```bash
./wrfall_install.py --install list            # list releases
./wrfall_install.py --install -arw            # newest release (or -chem -hydro -fire -fireideal -all)
./wrfall_install.py --install 4.6.0 -chem     # specific release
./wrfall_install.py --update                  # newer release, libraries reused
```

## 🗺️ Domain → data → run
```bash
./wrfall_install.py --mkdomain mydom                       # add -fire / -chem when several builds exist
cd ~/Build_WRF/mydom                                       # edit namelist.wps first
./wrfall --getdata gfs --date 20261016 --cycle 00 --end 72 # or ecmwf | era5
RUN_GEOGRID=1 ./wrfall --run 20261016 00 72 gfs            # or: --run auto
```

## 🎨 Build types
| Flag | Build | | Flag | Build |
|:---:|---|---|:---:|---|
| `-arw` | 🟢 WRF-ARW | | `-fire` | 🔴 WRF-Fire (real data) |
| `-chem` | 🟠 WRF-Chem | | `-fireideal` | 🟥 WRF-Fire (`em_fire`) |
| `-hydro` | 🔵 WRF-Hydro | | `-all` | 🟢🟡🔴 arw + chem + hydro + fire |

## 🧩 Extras
```bash
./wrfall_install.py --addpack domwiz,scripts,cdsapi   # Domain Wizard, utility scripts, ERA5 module
./wrfall_install.py --addgeog chem                    # also: mandatory | fire | any UCAR dataset
```

## 🌱 Load the environment
```bash
source ~/Build_WRF/etc/modulefiles/WRFARW.sh          # one build type at a time; see ~/WRF-DoList.txt
```

## 🚩 Handy flags
| Flag | Does |
|---|---|
| `--homedir DIR` | Install location (default `~/Build_WRF` or `$WRF_HOME`) |
| `--allyes` · `--dryrun` | No prompts · preview only |
| `--continue` · `--force` · `--force-libs` | Resume · rebuild WRF/WPS · rebuild libraries too |
| `--nogeog` · `--scour` | Skip WPS_GEOG · delete old release after update |
| `--dl-jobs N` · `--curl` · `--wget` · `--proxy H:P` | Download tuning |
| `--margin DEG` · `--bbox W E S N` | Data area: domain extent + margin, or explicit |
| `WRF_NP=8` · `REAL_NP=2` | MPI ranks for `wrf.exe` / `real.exe` |

## 📁 Layout
```text
~/Build_WRF/  local/ (libs)  packages/  logs/  etc/modulefiles/  WPS_GEOG/  Scripts/
              WRF-<ver>-<Type>/  WPS-<ver>-<Type>/
              mydom/  GFS_Input/YYYYMMDDCC/  wrf_output/{metgrid,ungrib,geogrid,Plots}/  wrfall -> script
```

## 🛟 Quick fixes
- **Interrupted?** Rerun with `--continue`; downloads resume.
- **Build failed?** Check `~/Build_WRF/logs/`; the *first* error is the cause.
- **WRF-Chem ≥ 4.7 KPP errors?** Use `--install 4.6.0 -chem --force`.
- **`--run` limit:** 1–72 forecast hours per call.

<p align="center"><i>Full details: <a href="WRFall_User_Guide.md">USER_GUIDE.md</a> · Now go forth and simulate! 🌍</i></p>
