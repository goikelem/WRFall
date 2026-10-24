#!/usr/bin/env python3
"""WRF/WPS Domain Wizard v2: interactive domain editor + namelist.wps / namelist.input generator.

Upgrades over v1
  * File/Edit menu, Ctrl+N / Ctrl+O / Ctrl+S / Ctrl+Z / Ctrl+Y / Delete / Esc
  * New project (starts in Draw Mode), Add Domain, Open (.json, namelist.wps, geo_em folder)
  * namelist.wps opening now draws the domains (extents are computed from the grid definition)
  * Persistent figure: boxes update in place (no flicker, view/zoom/basemap are preserved)
  * Click a domain (or its radio button) to select it; 8 resize handles (corners + edges)
  * Draw mode works on top of existing domains; drawn box stays selected with handles
  * Delete removes the *selected* domain (later domains shift down)
  * Undo/redo only records real changes (move/resize/draw/typed edits/add/delete/new/open)
  * Boxes follow map projection curvature (densified edges) with cartopy
  * Bug fixes: namelist.wps parser, per-domain dx, e_we/e_sn from geo_em, MAP_PROJ codes,
    ref_lon vs stand_lon, pan direction, figure leak
"""

import io
import json
import math
import re
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime
from pathlib import Path

try:
    import numpy as np
except ImportError:
    np = None

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.patches import Polygon
    from matplotlib.colors import to_rgba
    HAS_MPL = np is not None
except ImportError:
    HAS_MPL = False

try:
    import cartopy.crs as ccrs
    HAS_CARTOPY = HAS_MPL
except ImportError:
    ccrs = None
    HAS_CARTOPY = False

COLORS = ['purple', 'blue', 'red', 'green', 'orange', 'brown', 'teal', 'magenta', 'olive']
EXT_KEYS = ("minlat", "maxlat", "minlon", "maxlon")
ROW_KEYS = EXT_KEYS + ("resolution",)
DOM_KEYS = EXT_KEYS + ("cen_lat", "cen_lon", "resolution")
MIN_SPAN = 0.1          # degrees, minimum box size
HANDLE_PX = 11          # handle pick radius in pixels
PROJ_NAMES = {'lambert': 'Lambert Conformal', 'mercator': 'Mercator',
              'polar': 'Polar Stereographic', 'lat-lon': 'Latitude-Longitude'}
PROJ_CODES = {1: 'lambert', 2: 'polar', 3: 'mercator', 6: 'lat-lon'}
HANDLE_CURSORS = {'nw': 'top_left_corner', 'ne': 'top_right_corner',
                  'sw': 'bottom_left_corner', 'se': 'bottom_right_corner',
                  'n': 'sb_v_double_arrow', 's': 'sb_v_double_arrow',
                  'e': 'sb_h_double_arrow', 'w': 'sb_h_double_arrow'}


# ======================================================================
# Pure helpers (GUI independent, testable)
# ======================================================================
def parse_date(value, label):
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d_%H:%M:%S")
    except ValueError:
        raise ValueError(f"{label} must use YYYY-MM-DD_HH:MM:SS format.")


def even_at_least(value, minimum=20):
    value = max(minimum, int(math.ceil(value)))
    return value if value % 2 == 0 else value + 1


def align_dimension(value, ratio=3, minimum=20):
    value = max(minimum, int(math.ceil(value)))
    rem = (value - 1) % ratio
    if rem != 0:
        value += (ratio - rem)
    return value


def parse_namelist(text):
    """Fortran namelist -> {section: {key: [values]}} (lower-case keys, quotes stripped)."""
    sections = {}
    cur, buf = None, []

    def finish():
        body = "\n".join(buf)
        d = {}
        pat = r"(\w+)\s*(?:\([^)]*\))?\s*=\s*(.*?)(?=[\s,]+\w+\s*(?:\([^)]*\))?\s*=|\Z)"
        for m in re.finditer(pat, body, re.DOTALL):
            vals = re.findall(r"'[^']*'|\"[^\"]*\"|[^,\s]+", m.group(2))
            d[m.group(1).lower()] = [v.strip("'\"") for v in vals]
        sections[cur] = d

    for raw in text.splitlines():
        line = raw.split('!')[0]
        s = line.strip()
        if s.startswith('&'):
            if cur is not None:
                finish()
            cur, buf = s[1:].split()[0].lower(), []
        elif s in ('/', '&end') and cur is not None:
            finish()
            cur, buf = None, []
        elif cur is not None:
            if s.endswith('/') and '=' in s:       # "key = 1, /"
                buf.append(line.rstrip().rstrip('/'))
                finish()
                cur, buf = None, []
            else:
                buf.append(line)
    if cur is not None:
        finish()
    return sections


def parse_wps(text):
    secs = parse_namelist(text)
    share, geo, ungrib = secs.get('share', {}), secs.get('geogrid', {}), secs.get('ungrib', {})
    metg = secs.get('metgrid', {})

    def first(sec, key, default):
        v = sec.get(key)
        return v[0] if v else default

    def lst(sec, key, default, n):
        v = list(sec.get(key) or [])
        v += [default] * (n - len(v))
        return v[:n]

    n = int(first(share, 'max_dom', '1'))
    map_proj = first(geo, 'map_proj', 'lambert').lower()
    ref_lat = float(first(geo, 'ref_lat', '0'))
    ref_lon = float(first(geo, 'ref_lon', '0'))
    t1 = float(first(geo, 'truelat1', '0'))
    t2 = float(first(geo, 'truelat2', str(t1)))
    stand = float(first(geo, 'stand_lon', str(ref_lon)))
    dx0 = float(first(geo, 'dx', '30000'))
    dy0 = float(first(geo, 'dy', str(dx0)))
    pids = [int(float(x)) for x in lst(geo, 'parent_id', '1', n)]
    ratios = [int(float(x)) for x in lst(geo, 'parent_grid_ratio', '1', n)]
    ist = [int(float(x)) for x in lst(geo, 'i_parent_start', '1', n)]
    jst = [int(float(x)) for x in lst(geo, 'j_parent_start', '1', n)]
    ewe = [int(float(x)) for x in lst(geo, 'e_we', '1', n)]
    esn = [int(float(x)) for x in lst(geo, 'e_sn', '1', n)]

    dxs, dys = [], []
    for i in range(n):
        if i == 0:
            dxs.append(dx0)
            dys.append(dy0)
        else:
            p = max(0, min(i - 1, pids[i] - 1))
            r = max(1, ratios[i])
            dxs.append(dxs[p] / r)
            dys.append(dys[p] / r)

    metas = {}
    for i in range(n):
        metas[i] = {
            'e_we': ewe[i], 'e_sn': esn[i], 'dx': int(round(dxs[i])), 'dy': int(round(dys[i])),
            'mapproj': map_proj, 'truelat1': t1, 'truelat2': t2, 'stand_lon': stand,
            'cen_lat': ref_lat, 'cen_lon': ref_lon, 'moad_cen_lat': ref_lat,
            'parent_id': pids[i], 'i_parent_start': ist[i], 'j_parent_start': jst[i],
            'parent_grid_ratio': ratios[i],
        }
    return {
        'max_dom': n, 'map_proj': map_proj, 'geo_em_meta': metas,
        'start': first(share, 'start_date', ''), 'end': first(share, 'end_date', ''),
        'interval_seconds': first(share, 'interval_seconds', ''),
        'geog_data_path': first(geo, 'geog_data_path', ''),
        'geogrid_dir': first(share, 'opt_output_from_geogrid_path', ''),
        'metgrid_dir': first(metg, 'opt_output_from_metgrid_path', ''),
        'ungrib_prefix': first(ungrib, 'prefix', ''),
    }


def _make_to_lonlat(m0):
    """Return f(x, y) -> (lon, lat); x, y are metres from the centre of d01."""
    proj, ref_lat, ref_lon = m0['mapproj'], m0['cen_lat'], m0['cen_lon']
    t1, t2, stand = m0['truelat1'], m0['truelat2'], m0['stand_lon']
    if proj == 'lat-lon':
        return lambda x, y: (ref_lon + x, ref_lat + y)
    try:
        import pyproj
        sph = "+a=6370000 +b=6370000"
        if proj == 'lambert':
            s = f"+proj=lcc +lat_1={t1} +lat_2={t2} +lat_0={ref_lat} +lon_0={stand} {sph} +units=m +no_defs"
        elif proj == 'mercator':
            s = f"+proj=merc +lat_ts={t1} +lon_0={stand} {sph} +units=m +no_defs"
        else:
            s = (f"+proj=stere +lat_0={90 if ref_lat >= 0 else -90} +lat_ts={t1} "
                 f"+lon_0={stand} {sph} +units=m +no_defs")
        crs = pyproj.CRS.from_proj4(s)
        ll = pyproj.CRS.from_proj4(f"+proj=longlat {sph} +no_defs")
        fwd = pyproj.Transformer.from_crs(ll, crs, always_xy=True)
        inv = pyproj.Transformer.from_crs(crs, ll, always_xy=True)
        xc, yc = fwd.transform(ref_lon, ref_lat)
        return lambda x, y: inv.transform(xc + x, yc + y)
    except Exception:
        c = max(0.05, math.cos(math.radians(ref_lat)))
        return lambda x, y: (ref_lon + x / (111320.0 * c), ref_lat + y / 111320.0)


def compute_domain_extents(metas, n):
    """{i: (minlat, maxlat, minlon, maxlon)} from WPS grid definition."""
    to_ll = _make_to_lonlat(metas[0])
    boxes, out = {}, {}
    for i in range(n):
        m = metas[i]
        w, h = (m['e_we'] - 1) * m['dx'], (m['e_sn'] - 1) * m['dy']
        if i == 0:
            x0, y0 = -w / 2.0, -h / 2.0
        else:
            p = m['parent_id'] - 1
            if p not in boxes:
                p = i - 1
            pb, pm = boxes[p], metas[p]
            x0 = pb[0] + (m['i_parent_start'] - 1) * pm['dx']
            y0 = pb[2] + (m['j_parent_start'] - 1) * pm['dy']
        boxes[i] = (x0, x0 + w, y0, y0 + h)
        pts = []
        for k in range(21):
            f = k / 20.0
            pts += [(x0 + f * w, y0), (x0 + f * w, y0 + h), (x0, y0 + f * h), (x0 + w, y0 + f * h)]
        lls = [to_ll(x, y) for x, y in pts]
        lons = [p[0] for p in lls]
        lats = [p[1] for p in lls]
        out[i] = (min(lats), max(lats), min(lons), max(lons))
    return out


# ======================================================================
# GUI
# ======================================================================
class DomainGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WRF/WPS Domain Namelist Generator & Interactive Wizard")
        self.geometry("1240x980")
        self.minsize(1000, 820)
        self.configure(bg="#eef3f8")
        self.use_cartopy = HAS_CARTOPY

        # ---- state (initialised before any widget is built) ----
        self.undo_stack, self.redo_stack = [], []
        self._suspend_undo = False
        self._entry_snap = None
        self._overlay_job = None
        self.domain_vars = []
        self.geo_em_meta = {}
        self.selected = None
        self.interaction = None      # move / resize in progress
        self.drawing = None          # rubber-band draw in progress
        self.preview_poly = None
        self.panning = False
        self.pan_state = None
        self.fig = self.ax = self.canvas = None
        self.canvas_mode = None      # 'map' | 'terrain'
        self._fig_proj = None
        self.domain_artists = {}
        self.handle_line = None
        self.legend = None
        self.basemap_artist = None
        self.basemap_cache = None
        self._cursor = None

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#eef3f8")
        style.configure("Card.TFrame", background="white")
        style.configure("TLabel", background="#eef3f8", font=("Arial", 10))
        style.configure("Card.TLabel", background="white", font=("Arial", 10))
        style.configure("Card.TRadiobutton", background="white", font=("Arial", 9, "bold"))
        style.configure("Title.TLabel", background="#17365d", foreground="white",
                        font=("Arial", 17, "bold"))
        style.configure("Section.TLabel", background="white", foreground="#17365d",
                        font=("Arial", 11, "bold"))
        style.configure("TButton", font=("Arial", 10, "bold"), padding=6)
        style.configure("Generate.TButton", background="#1976d2", foreground="white")
        style.map("Generate.TButton", background=[("active", "#125ca1")])

        self.sel_var = tk.IntVar(value=-1)
        self._build_menu()

        header = tk.Frame(self, bg="#17365d", height=70)
        header.pack(fill="x")
        ttk.Label(header, text="WRF / WPS DOMAIN GENERATOR & WIZARD",
                  style="Title.TLabel").pack(anchor="w", padx=22, pady=18)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab_config = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_config, text=" \u2699\ufe0f Domain Configuration ")
        self.setup_config_tab()
        self.tab_preview = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_preview, text=" \U0001f5fa\ufe0f Interactive Domain Wizard ")
        self.setup_preview_tab()
        self.tab_editor = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_editor, text=" \U0001f4dd Namelist Editor ")
        self.setup_editor_tab()

        self.ctx_menu = tk.Menu(self, tearoff=0)
        self.ctx_menu.add_command(label="Delete this domain", command=self.delete_domain)
        self.ctx_menu.add_command(label="Deselect", command=lambda: self._select(None))

        self.bind("<Control-z>", lambda e: self.undo())
        self.bind("<Control-y>", lambda e: self.redo())
        self.bind("<Control-n>", lambda e: self.new_domain())
        self.bind("<Control-o>", lambda e: self.open_domain())
        self.bind("<Control-s>", lambda e: self.save_domain())
        self.bind("<Delete>", self._on_delete_key)
        self.bind("<Escape>", self._on_escape)

    # ------------------------------------------------------------------
    def _build_menu(self):
        mb = tk.Menu(self)
        fm = tk.Menu(mb, tearoff=0)
        fm.add_command(label="New domain project\tCtrl+N", command=self.new_domain)
        fm.add_command(label="Open existing domain\u2026\tCtrl+O", command=self.open_domain)
        fm.add_command(label="Open geo_em folder\u2026", command=self.load_from_geo_em)
        fm.add_command(label="Save domain\u2026\tCtrl+S", command=self.save_domain)
        fm.add_separator()
        fm.add_command(label="Generate namelists", command=self.generate)
        fm.add_separator()
        fm.add_command(label="Quit", command=self.destroy)
        mb.add_cascade(label="File", menu=fm)
        em = tk.Menu(mb, tearoff=0)
        em.add_command(label="Undo\tCtrl+Z", command=self.undo)
        em.add_command(label="Redo\tCtrl+Y", command=self.redo)
        em.add_separator()
        em.add_command(label="Add domain", command=self.add_domain)
        em.add_command(label="Delete selected domain\tDel", command=self.delete_domain)
        mb.add_cascade(label="Edit", menu=em)
        self.config(menu=mb)

    # ==================================================================
    # Snapshots / undo / redo
    # ==================================================================
    def _rows(self):
        return [{k: d[k].get() for k in ROW_KEYS} for d in self.domain_vars]

    def _snapshot(self):
        return {
            'version': 2,
            'maxdom': str(len(self.domain_vars)),
            'proj': self.proj.get(),
            'geo_em_meta': {str(k): dict(v) for k, v in self.geo_em_meta.items()},
            'domains': [{k: d[k].get() for k in DOM_KEYS} for d in self.domain_vars],
        }

    def _push_snap(self, snap):
        if self._suspend_undo:
            return
        self.undo_stack.append(snap)
        if len(self.undo_stack) > 100:
            self.undo_stack.pop(0)
        self.redo_stack.clear()

    def _push_undo(self):
        self._push_snap(self._snapshot())

    def _apply_rows(self, rows):
        """Rebuild the domain table with the given rows (list of dicts)."""
        self.maxdom.set(str(max(1, len(rows))))
        self.update_domain_inputs(keep=False)
        for i, r in enumerate(rows[:len(self.domain_vars)]):
            for k in ROW_KEYS:
                self.domain_vars[i][k].set(r.get(k, ''))

    def _restore(self, snap):
        self._suspend_undo = True
        try:
            rows = [{k: d.get(k, '') for k in ROW_KEYS} for d in snap['domains']]
            self._apply_rows(rows)
            self.proj.set(snap.get('proj', 'Lambert Conformal'))
            self.geo_em_meta = {int(k): dict(v) for k, v in snap.get('geo_em_meta', {}).items()}
            self._select(None, quiet=True)
        finally:
            self._suspend_undo = False
        self.update_overlays()

    def undo(self):
        if not self.undo_stack:
            self.status.set("Nothing to undo.")
            return
        self.redo_stack.append(self._snapshot())
        self._restore(self.undo_stack.pop())
        self.status.set(f"Undo ({len(self.undo_stack)} more available).")

    def redo(self):
        if not self.redo_stack:
            self.status.set("Nothing to redo.")
            return
        self.undo_stack.append(self._snapshot())
        self._restore(self.redo_stack.pop())
        self.status.set(f"Redo ({len(self.redo_stack)} more available).")

    # ==================================================================
    # New / Add / Delete / Open / Save
    # ==================================================================
    def _has_work(self):
        return any(self._bounds(i) for i in range(len(self.domain_vars)))

    def new_domain(self):
        if self._has_work() and not messagebox.askyesno(
                "New domain project",
                "Start a new domain project?\n(You can still press Undo to get the old one back.)"):
            return
        self._push_undo()
        self._suspend_undo = True
        try:
            self._apply_rows([{'resolution': '15'}])
            self.proj.set("Lambert Conformal")
            self.geo_em_meta = {}
            self._select(None, quiet=True)
        finally:
            self._suspend_undo = False
        self.notebook.select(self.tab_preview)
        self.draw_mode_var.set(True)
        self.target_domain_var.set("d01")
        self.on_draw_mode_toggle()
        self.plot_domain_boxes()
        self.status.set("New project. Draw Mode is ON: drag a rectangle on the map for d01.")

    def add_domain(self):
        n = len(self.domain_vars)
        if n >= 9:
            messagebox.showinfo("Add domain", "WRF/WPS wizard supports up to 9 domains.")
            return
        self._push_undo()
        rows = self._rows()
        try:
            res = f"{float(rows[-1]['resolution']) / 3:.4g}"
        except (ValueError, IndexError):
            res = "5"
        rows.append({'resolution': res})
        self._suspend_undo = True
        try:
            self._apply_rows(rows)
        finally:
            self._suspend_undo = False
        self._select(n, quiet=True)
        self.draw_mode_var.set(True)
        self.target_domain_var.set(f"d{n + 1:02d}")
        self.on_draw_mode_toggle()
        self.update_overlays()
        self.status.set(f"Added d{n + 1:02d} ({res} km). Draw Mode ON: drag a box inside the parent.")

    def delete_domain(self):
        sel = self.selected
        if sel is None or sel >= len(self.domain_vars):
            messagebox.showinfo(
                "Delete domain",
                "Select a domain first: click it on the map, or tick its radio button "
                "in the extents table. Then press Delete / use Delete Domain.")
            return
        n = len(self.domain_vars)
        name = f"d{sel + 1:02d}"
        if n == 1:
            if not messagebox.askyesno("Delete domain", "d01 is the only domain. Clear its extents?"):
                return
            self._push_undo()
            for k in EXT_KEYS:
                self.domain_vars[0][k].set("")
            self.geo_em_meta = {}
            self._select(None, quiet=True)
            self.update_overlays()
            self.status.set("Cleared d01 extents.")
            return
        later = (f" d{sel + 2:02d}\u2013d{n:02d} will be renumbered." if sel < n - 1 else "")
        if not messagebox.askyesno("Delete domain",
                                   f"Delete {name}? max_dom becomes {n - 1}.{later}"):
            return
        self._push_undo()
        rows = self._rows()
        del rows[sel]
        self._suspend_undo = True
        try:
            self._apply_rows(rows)
            self.geo_em_meta = {k: v for k, v in self.geo_em_meta.items() if k < sel}
            self._select(None, quiet=True)
        finally:
            self._suspend_undo = False
        self.update_overlays()
        self.status.set(f"Deleted {name}. max_dom = {n - 1}.")

    def _on_delete_key(self, event):
        w = self.focus_get()
        if isinstance(w, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox)):
            return
        if self.notebook.select() == str(self.tab_preview):
            self.delete_domain()

    def _on_escape(self, event=None):
        self._cancel_drawing()
        self._select(None)

    # ------------------------------------------------------------------
    def open_domain(self):
        path = filedialog.askopenfilename(
            title="Open existing domain",
            filetypes=[("Domain files", "*.json *.wps"), ("JSON domain file", "*.json"),
                       ("WPS namelist", "namelist*"), ("All files", "*.*")],
            initialdir=str(Path(self.outdir.get()).expanduser()))
        if not path:
            return
        try:
            if path.lower().endswith('.json'):
                self._open_json(path)
            else:
                self._open_wps(path)
        except Exception as exc:
            messagebox.showerror("Open error", f"{type(exc).__name__}: {exc}")

    def _apply_timing_paths(self, timing=None, paths=None):
        for k, v in (timing or {}).items():
            if k in self.vars and v:
                self.vars[k].set(v)
        pm = {'outdir': self.outdir, 'geog': self.geog, 'geogrid_dir': self.geogrid_dir,
              'ungrib_prefix': self.ungrib_prefix, 'metgrid_dir': self.metgrid_dir}
        for k, v in (paths or {}).items():
            if k in pm and v:
                pm[k].set(v)

    def _open_json(self, path):
        with open(path, 'r') as f:
            data = json.load(f)
        self._push_undo()
        self._restore(data)
        self._apply_timing_paths(data.get('timing'), data.get('paths'))
        self.plot_domain_boxes()
        self.status.set(f"Opened {Path(path).name}: {len(self.domain_vars)} domain(s).")

    def _open_wps(self, path):
        p = parse_wps(Path(path).read_text())
        n = p['max_dom']
        ext = compute_domain_extents(p['geo_em_meta'], n)
        self._push_undo()
        rows = []
        for i in range(n):
            a, b, c, d = ext[i]
            rows.append({'minlat': f"{a:.4f}", 'maxlat': f"{b:.4f}", 'minlon': f"{c:.4f}",
                         'maxlon': f"{d:.4f}",
                         'resolution': f"{p['geo_em_meta'][i]['dx'] / 1000.0:.4g}"})
        self._suspend_undo = True
        try:
            self._apply_rows(rows)
            self.proj.set(PROJ_NAMES.get(p['map_proj'], 'Lambert Conformal'))
            self.geo_em_meta = p['geo_em_meta']
            self._select(None, quiet=True)
        finally:
            self._suspend_undo = False
        if p['start']:
            self.vars['start'].set(p['start'])
        if p['end']:
            self.vars['end'].set(p['end'])
        try:
            self.vars['input_interval'].set(str(int(float(p['interval_seconds'])) // 3600))
        except ValueError:
            pass
        self._apply_timing_paths(None, {
            'geog': p['geog_data_path'], 'geogrid_dir': p['geogrid_dir'].rstrip('/'),
            'metgrid_dir': p['metgrid_dir'].rstrip('/'), 'ungrib_prefix': p['ungrib_prefix']})
        self.notebook.select(self.tab_preview)
        self.plot_domain_boxes()
        self.status.set(f"Opened {Path(path).name}: {n} domain(s). Extents were computed from the "
                        "grid; generating reproduces the exact grid until you edit a box.")

    def save_domain(self):
        path = filedialog.asksaveasfilename(
            title="Save domain configuration", defaultextension=".json",
            filetypes=[("JSON domain file", "*.json")],
            initialdir=str(Path(self.outdir.get()).expanduser()))
        if not path:
            return
        try:
            data = self._snapshot()
            data['timing'] = {k: v.get() for k, v in self.vars.items()}
            data['paths'] = {'outdir': self.outdir.get(), 'geog': self.geog.get(),
                             'geogrid_dir': self.geogrid_dir.get(),
                             'ungrib_prefix': self.ungrib_prefix.get(),
                             'metgrid_dir': self.metgrid_dir.get()}
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
            self.status.set(f"Saved domain config to {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save error", str(exc))

    # ==================================================================
    # Config tab
    # ==================================================================
    def setup_config_tab(self):
        outer = ttk.Frame(self.tab_config, padding=(14, 14))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        top = ttk.Frame(outer)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)
        proj_card = ttk.Frame(top, style="Card.TFrame", padding=14)
        dates_card = ttk.Frame(top, style="Card.TFrame", padding=14)
        proj_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        dates_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        ttk.Label(proj_card, text="Projection and domain count",
                  style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(proj_card, text="Map projection", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.proj = tk.StringVar(value="Lambert Conformal")
        cb = ttk.Combobox(proj_card, textvariable=self.proj, state="readonly",
                          values=list(PROJ_NAMES.values()))
        cb.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.plot_domain_boxes())
        ttk.Label(proj_card, text="Number of domains (max_dom)",
                  style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=4)
        self.maxdom = tk.StringVar(value="3")
        mcb = ttk.Combobox(proj_card, textvariable=self.maxdom, state="readonly",
                           values=[str(i) for i in range(1, 10)])
        mcb.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=4)
        mcb.bind("<<ComboboxSelected>>", self._on_maxdom_change)
        proj_card.columnconfigure(1, weight=1)

        ttk.Label(dates_card, text="Simulation timing", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.vars = {}
        for row, (key, label, default) in enumerate([
                ("start", "Simulation start", "2026-08-01_00:00:00"),
                ("end", "Simulation end", "2026-08-03_00:00:00"),
                ("input_interval", "Met input interval (hours)", "6"),
                ("time_step", "WRF timestep (seconds)", "30"),
                ("history_interval", "History output (minutes)", "60")], start=1):
            ttk.Label(dates_card, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=default)
            self.vars[key] = var
            ttk.Entry(dates_card, textvariable=var, width=20).grid(row=row, column=1, sticky="ew",
                                                                   padx=(10, 0), pady=4)
        dates_card.columnconfigure(1, weight=1)

        self.extent_card = ttk.Frame(outer, style="Card.TFrame", padding=14)
        self.extent_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(self.extent_card,
                  text="Domain extents, centre and resolution  (tick a radio button to select a domain)",
                  style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.domains_container = ttk.Frame(self.extent_card, style="Card.TFrame")
        self.domains_container.grid(row=1, column=0, sticky="nsew")
        self.extent_card.columnconfigure(0, weight=1)
        self.update_domain_inputs(keep=False)

        pc = ttk.Frame(outer, style="Card.TFrame", padding=14)
        pc.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(pc, text="Output and geography data paths", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        home = Path.home()
        self.outdir = tk.StringVar(value=str(home / "domains"))
        self.geog = tk.StringVar(value=str(home / "WRF_GEOG"))
        self.geogrid_dir = tk.StringVar(value=str(home / "Build_WRF" / "wrf_output" / "geogrid"))
        self.ungrib_prefix = tk.StringVar(value=str(home / "Build_WRF" / "wrf_output" / "ungrib" / "FILE"))
        self.metgrid_dir = tk.StringVar(value=str(home / "Build_WRF" / "wrf_output" / "metgrid"))
        rows = [("Output directory", self.outdir, lambda: self._pick_dir(self.outdir)),
                ("WPS geog_data_path", self.geog, lambda: self._pick_dir(self.geog)),
                ("Geogrid output directory", self.geogrid_dir, lambda: self._pick_dir(self.geogrid_dir)),
                ("Ungrib FILE prefix", self.ungrib_prefix, self.choose_ungrib_prefix),
                ("Metgrid output directory", self.metgrid_dir, lambda: self._pick_dir(self.metgrid_dir))]
        for r, (lab, var, cmd) in enumerate(rows, start=1):
            ttk.Label(pc, text=lab, style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(pc, textvariable=var).grid(row=r, column=1, sticky="ew", padx=8, pady=3)
            ttk.Button(pc, text="Browse\u2026", command=cmd).grid(row=r, column=2, pady=3)
        pc.columnconfigure(1, weight=1)

        act = ttk.Frame(outer)
        act.grid(row=3, column=0, sticky="ew", pady=(4, 6))
        ttk.Button(act, text="Generate namelists", style="Generate.TButton",
                   command=self.generate).pack(side="left")
        for txt, cmd in [("New", self.new_domain), ("Open\u2026", self.open_domain),
                         ("Save Domain\u2026", self.save_domain), ("Undo", self.undo),
                         ("Redo", self.redo), ("Add Domain", self.add_domain),
                         ("Delete Domain", self.delete_domain)]:
            ttk.Button(act, text=txt, command=cmd).pack(side="left", padx=3)
        ttk.Button(act, text="Load Example", command=self.load_example).pack(side="left", padx=8)

        model_card = ttk.Frame(outer, style="Card.TFrame", padding=12)
        model_card.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(model_card, text="Optional model components (verify against your WRF build)",
                  style="Section.TLabel").grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 6))
        self.model_flags = {
            "chemistry": tk.BooleanVar(value=False),
            "hydro": tk.BooleanVar(value=False),
            "fire": tk.BooleanVar(value=False),
        }
        ttk.Label(model_card, text="Configuration profile", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.model_profile = tk.StringVar(value="Custom / manual settings")
        self.model_profile_cb = ttk.Combobox(
            model_card, textvariable=self.model_profile, state="readonly", width=32,
            values=("Custom / manual settings", "WRF-Chem", "WRF-Hydro",
                    "Fire / fire emissions", "WRF-Chem + Fire"))
        self.model_profile_cb.grid(row=1, column=1, columnspan=3, sticky="w", padx=(0, 12), pady=3)
        self.model_profile_cb.bind("<<ComboboxSelected>>", self._on_model_profile_change)
        ttk.Label(model_card, text="Component option lists below:", style="Card.TLabel").grid(
            row=1, column=4, columnspan=4, sticky="w", padx=(8, 4), pady=3)
        for col, (key, label) in enumerate([
            ("chemistry", "WRF-Chem chemistry"),
            ("hydro", "WRF-Hydro coupling"),
            ("fire", "Fire / fire emissions"),
        ]):
            ttk.Checkbutton(model_card, text=label, variable=self.model_flags[key]).grid(
                row=2, column=col * 2, columnspan=2, sticky="w", padx=(0, 10), pady=3)
        self.model_values = {}
        model_defaults = [
            # WRF-Chem &chem options (names follow the user's requested namelist example).
            ("chem_opt", "chem_opt", "112"),
            ("chem_in_opt", "chem_in_opt", "1"),
            ("bio_emiss_opt", "bio_emiss_opt", "3"),
            ("emiss_opt", "emiss_opt", "5"),
            ("emiss_inpt_opt", "emiss_inpt_opt", "1"),
            ("dust_opt", "dust_opt", "1"),
            ("seas_opt", "seas_opt", "1"),
            ("phot_opt", "phot_opt", "1"),
            ("gas_drydep_opt", "gas_drydep_opt", "1"),
            ("aer_drydep_opt", "aer_drydep_opt", "1"),
            ("aer_rad_feedback", "aer_rad_feedback", "1"),
            # Additional fire-related controls retained for builds that support them.
            ("ifire", "ifire", "2"),
            ("fire_fuel_read", "fire_fuel_read", "1"),
            ("fire_fuel_cat", "fire_fuel_cat", "3"),
            ("sr_x", "sr_x", "10"),
            ("sr_y", "sr_y", "10"),
            ("fire_wind_height", "fire_wind_height", "6.0"),
            ("fire_num_ignitions", "fire_num_ignitions", "1"),
            ("fire_ignition_start_x1", "fire_ignition_start_x1", "-108.25"),
            ("fire_ignition_end_x1", "fire_ignition_end_x1", "-108.25"),
            ("fire_ignition_start_y1", "fire_ignition_start_y1", "38.50"),
            ("fire_ignition_end_y1", "fire_ignition_end_y1", "38.52"),
            ("fire_ignition_start_time1", "fire_ignition_start_time1", "3600"),
            ("fire_ignition_end_time1", "fire_ignition_end_time1", "3900"),
            ("fire_ignition_radius1", "fire_ignition_radius1", "50"),
            ("sf_urban_physics", "sf_urban_physics", "0"),
        ]
        for idx, (key, label, default) in enumerate(model_defaults):
            rr, cc = 3 + idx // 4, (idx % 4) * 2
            ttk.Label(model_card, text=label, style="Card.TLabel").grid(
                row=rr, column=cc, sticky="w", padx=(0, 4), pady=2)
            var = tk.StringVar(value=default)
            self.model_values[key] = var
            ttk.Entry(model_card, textvariable=var, width=10).grid(
                row=rr, column=cc + 1, sticky="w", padx=(0, 8), pady=2)
        dropdown_row = 3 + (len(model_defaults) + 3) // 4
        ttk.Label(model_card, text="Chem option", style="Card.TLabel").grid(
            row=dropdown_row, column=0, sticky="w", padx=(0, 4), pady=(6, 2))
        self.chem_option_var = tk.StringVar(value="chem_opt")
        self.chem_option_cb = ttk.Combobox(model_card, textvariable=self.chem_option_var,
            state="readonly", width=22, values=("chem_opt", "chem_in_opt", "bio_emiss_opt",
            "emiss_opt", "emiss_inpt_opt", "dust_opt", "seas_opt", "phot_opt",
            "gas_drydep_opt", "aer_drydep_opt", "aer_rad_feedback"))
        self.chem_option_cb.grid(row=dropdown_row, column=1, columnspan=2, sticky="w", pady=(6, 2))
        self.chem_option_cb.bind("<<ComboboxSelected>>", lambda _e: self._select_model_option("chemistry", self.chem_option_var.get()))

        ttk.Label(model_card, text="Hydro file / option", style="Card.TLabel").grid(
            row=dropdown_row, column=3, sticky="w", padx=(8, 4), pady=(6, 2))
        self.hydro_option_var = tk.StringVar(value="hydro.namelist")
        self.hydro_option_cb = ttk.Combobox(model_card, textvariable=self.hydro_option_var,
            state="readonly", width=29, values=("hydro.namelist", "namelist.hrldas",
            "restart_file", "outdir", "rst_dt", "out_dt", "channel_option",
            "route_lsm", "route_kinematic", "udt", "dx", "aggfact", "geo_finegrid_flnm"))
        self.hydro_option_cb.grid(row=dropdown_row, column=4, columnspan=2, sticky="w", pady=(6, 2))
        self.hydro_option_cb.bind("<<ComboboxSelected>>", lambda _e: self._select_model_option("hydro", self.hydro_option_var.get()))

        ttk.Label(model_card, text="Fire option", style="Card.TLabel").grid(
            row=dropdown_row, column=6, sticky="w", padx=(8, 4), pady=(6, 2))
        self.fire_option_var = tk.StringVar(value="ifire")
        self.fire_option_cb = ttk.Combobox(model_card, textvariable=self.fire_option_var,
            state="readonly", width=20, values=("ifire", "fire_fuel_read", "fire_fuel_cat",
            "sr_x", "sr_y", "fire_wind_height", "fire_num_ignitions",
            "fire_ignition_start_x1", "fire_ignition_end_x1", "fire_ignition_start_y1",
            "fire_ignition_end_y1", "fire_ignition_start_time1", "fire_ignition_end_time1",
            "fire_ignition_radius1"))
        self.fire_option_cb.grid(row=dropdown_row, column=7, sticky="w", pady=(6, 2))
        self.fire_option_cb.bind("<<ComboboxSelected>>", lambda _e: self._select_model_option("fire", self.fire_option_var.get()))

        ttk.Label(model_card, text=(
            "WRF-Chem options are emitted in &chem; fire ignition/fuel settings are emitted in &fire. "
            "When Hydro is enabled, the wizard also writes hydro.namelist and namelist.hrldas templates. "
            "Hydro templates and optional namelist keys must be checked against your exact WRF/WRF-Hydro release."),
            style="Card.TLabel", wraplength=1050, justify="left").grid(
                row=dropdown_row + 1, column=0, columnspan=8, sticky="w", pady=(6, 0))

        sc = ttk.Frame(outer, style="Card.TFrame", padding=10)
        sc.grid(row=5, column=0, sticky="nsew")
        ttk.Label(sc, text="Status / summary", style="Section.TLabel").pack(anchor="w")
        self.status = tk.StringVar(value="Ready. Use New, Open\u2026 or Load Example to begin.")
        ttk.Label(sc, textvariable=self.status, style="Card.TLabel",
                  wraplength=1000, justify="left").pack(anchor="w", pady=(4, 0))
        outer.rowconfigure(5, weight=1)

    def _on_model_profile_change(self, _event=None):
        """Apply a component preset and expose the relevant namelist option dropdowns."""
        profile = self.model_profile.get()
        presets = {
            "Custom / manual settings": None,
            "WRF-Chem": {"chemistry": True, "hydro": False, "fire": False},
            "WRF-Hydro": {"chemistry": False, "hydro": True, "fire": False},
            "Fire / fire emissions": {"chemistry": False, "hydro": False, "fire": True},
            "WRF-Chem + Fire": {"chemistry": True, "hydro": False, "fire": True},
        }
        selected = presets.get(profile)
        if selected is not None:
            for key, enabled in selected.items():
                self.model_flags[key].set(enabled)
            # Seed a non-zero chemistry selection as a reminder to choose a scheme supported
            # by the user's compiled WRF-Chem configuration; do not force a scheme ID.
            if selected.get("chemistry") and self.model_values.get("chem_opt", tk.StringVar(value="0")).get() == "0":
                self.model_values["chem_opt"].set(0)
        self.status.set(
            f"Profile selected: {profile}. Review all option values against your WRF build and "
            "the official README.namelist before generating or running the model."
        )

    def _select_model_option(self, component, option_key):
        """Focus the matching editable control or explain where a separate file is required."""
        key_to_var = {key: key for key in self.model_values}
        if component == "hydro":
            self.model_flags["hydro"].set(True)
            self.status.set(
                f"Selected {option_key}. The wizard will write hydro.namelist and namelist.hrldas "
                "templates when generating outputs; verify the fields against your installed release.")
            return
        self.model_flags[component].set(True)
        variable = key_to_var.get(option_key)
        if variable:
            self.model_values[variable].set(self.model_values[variable].get())
            self.status.set(f"Selected {option_key}. Edit its value in the namelist option fields above.")

    def _pick_dir(self, var):
        sel = filedialog.askdirectory(initialdir=var.get() or str(Path.home()))
        if sel:
            var.set(sel)

    def choose_ungrib_prefix(self):
        p = Path(self.ungrib_prefix.get()).expanduser()
        sel = filedialog.asksaveasfilename(title="Choose ungrib FILE prefix",
                                           initialdir=str(p.parent), initialfile=p.name)
        if sel:
            self.ungrib_prefix.set(sel)

    # ------------------------------------------------------------------
    def _on_maxdom_change(self, event=None):
        old_n = len(self.domain_vars)
        new_n = int(self.maxdom.get())
        if new_n == old_n:
            return
        self._push_undo()                      # snapshot still reflects old_n rows
        self.geo_em_meta = {k: v for k, v in self.geo_em_meta.items() if k < new_n}
        self.update_domain_inputs(keep=True)
        if self.selected is not None and self.selected >= new_n:
            self._select(None, quiet=True)
        self.update_overlays()

    def update_domain_inputs(self, keep=True):
        old = self._rows() if keep else []
        for w in self.domains_container.winfo_children():
            w.destroy()
        try:
            n = int(self.maxdom.get())
        except ValueError:
            n = 1
        self.domain_vars = []
        headers = ["Domain", "Min Lat", "Max Lat", "Min Lon", "Max Lon", "Cen Lat", "Cen Lon",
                   "Resolution (km)"]
        for c, h in enumerate(headers):
            ttk.Label(self.domains_container, text=h, style="Card.TLabel",
                      font=("Arial", 9, "bold")).grid(row=0, column=c, padx=3, pady=2, sticky="w")
        for i in range(n):
            ttk.Radiobutton(self.domains_container, text=f"d{i + 1:02d}", value=i,
                            variable=self.sel_var, style="Card.TRadiobutton",
                            command=lambda i=i: self._select(i)).grid(row=i + 1, column=0,
                                                                      padx=3, pady=4, sticky="w")
            prev = old[i] if i < len(old) else {}
            if 'resolution' in prev and prev['resolution']:
                res = prev['resolution']
            elif i == 0:
                res = "15"
            else:
                try:
                    res = f"{float(self.domain_vars[i - 1]['resolution'].get()) / 3:.4g}"
                except ValueError:
                    res = "5"
            vd = {}
            for c, key in enumerate(DOM_KEYS, start=1):
                if key in ("cen_lat", "cen_lon"):
                    v = tk.StringVar(value="")
                    ttk.Entry(self.domains_container, textvariable=v, width=10,
                              state="readonly").grid(row=i + 1, column=c, padx=3, pady=4, sticky="ew")
                else:
                    v = tk.StringVar(value=res if key == "resolution" else prev.get(key, ""))
                    e = ttk.Entry(self.domains_container, textvariable=v, width=10)
                    e.grid(row=i + 1, column=c, padx=3, pady=4, sticky="ew")
                    e.bind("<FocusIn>", lambda ev: self._entry_in())
                    e.bind("<FocusOut>", lambda ev, i=i: self._entry_out(i))
                    e.bind("<Return>", lambda ev, i=i: self._entry_out(i))
                vd[key] = v
                self.domains_container.columnconfigure(c, weight=1)
            for k in EXT_KEYS:
                vd[k].trace_add("write", lambda *a, vd=vd: self._on_ext_change(vd))
            self.domain_vars.append(vd)
            self._update_center(vd)
        self.sel_var.set(-1 if self.selected is None else self.selected)
        self._refresh_target_dropdown()

    def _update_center(self, dvar):
        try:
            a, b = float(dvar["minlat"].get()), float(dvar["maxlat"].get())
            c, d = float(dvar["minlon"].get()), float(dvar["maxlon"].get())
            dvar["cen_lat"].set(f"{(a + b) / 2:.4f}")
            dvar["cen_lon"].set(f"{(c + d) / 2:.4f}")
        except (ValueError, TypeError):
            dvar["cen_lat"].set("")
            dvar["cen_lon"].set("")

    def _on_ext_change(self, dvar):
        self._update_center(dvar)
        self._schedule_overlay()

    def _entry_in(self):
        self._entry_snap = self._snapshot()

    def _entry_out(self, idx):
        snap, self._entry_snap = self._entry_snap, None
        if snap is None:
            return
        cur = self._snapshot()
        if snap['domains'] != cur['domains']:
            self._push_snap(snap)
            self.geo_em_meta = {k: v for k, v in self.geo_em_meta.items() if k < idx}
            self.update_overlays()

    def _schedule_overlay(self):
        if self._overlay_job:
            self.after_cancel(self._overlay_job)
        self._overlay_job = self.after(150, self._overlay_tick)

    def _overlay_tick(self):
        self._overlay_job = None
        if self.canvas_mode == 'map' and self.ax is not None and not self.interaction \
                and not self.drawing:
            self._update_artists()
            self.canvas.draw_idle()

    # ==================================================================
    # Preview tab
    # ==================================================================
    def setup_preview_tab(self):
        pf = ttk.Frame(self.tab_preview, padding=14)
        pf.pack(fill="both", expand=True)
        ttk.Label(pf, text="Interactive Domain Wizard & Extent Resizer",
                  font=("Arial", 12, "bold")).pack(anchor="w", pady=(0, 4))
        hint = ("Click = select  |  Drag inside = move  |  Drag handles (corners/edges) = resize  |  "
                "Del = delete selected  |  Esc = deselect  |  Scroll = zoom  |  Drag empty = pan  |  "
                "Right-click = menu  |  Projection: "
                + ("cartopy (projected)" if self.use_cartopy else "plain lat/lon"))
        ttk.Label(pf, text=hint).pack(anchor="w", pady=(0, 8))

        r1 = ttk.Frame(pf)
        r1.pack(fill="x", pady=(0, 4))
        self.edit_mode_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r1, text="Interactive Editing", variable=self.edit_mode_var,
                        command=self.update_overlays).pack(side="left")
        for txt, cmd in [("Refresh Map", self.plot_domain_boxes), ("Plot Terrain", self.plot_terrain),
                         ("Load geo_em", self.load_from_geo_em), ("Undo", self.undo), ("Redo", self.redo),
                         ("Add Domain", self.add_domain), ("Delete Selected", self.delete_domain)]:
            ttk.Button(r1, text=txt, command=cmd).pack(side="left", padx=4)

        r2 = ttk.Frame(pf)
        r2.pack(fill="x", pady=(0, 8))
        df = ttk.LabelFrame(r2, text="Draw by mouse")
        df.pack(side="left", padx=(0, 10))
        self.draw_mode_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(df, text="Draw Mode", variable=self.draw_mode_var,
                        command=self.on_draw_mode_toggle).pack(side="left", padx=4)
        ttk.Label(df, text="Target:").pack(side="left", padx=2)
        self.target_domain_var = tk.StringVar(value="d01")
        self.target_domain_cb = ttk.Combobox(df, textvariable=self.target_domain_var,
                                             state="readonly", width=5)
        self.target_domain_cb.pack(side="left", padx=4)
        self.target_domain_cb.bind("<<ComboboxSelected>>", lambda e: self._select(self._target_index()))
        self.constrain_to_parent_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(df, text="Keep inside parent", variable=self.constrain_to_parent_var).pack(
            side="left", padx=6)
        ttk.Button(df, text="Cancel draw", command=self._cancel_drawing).pack(side="left", padx=4)

        bf = ttk.LabelFrame(r2, text="Basemap")
        bf.pack(side="left", padx=(0, 10))
        self.basemap_var = tk.StringVar(value="None")
        ttk.Combobox(bf, textvariable=self.basemap_var, state="readonly", width=18,
                     values=["None", "ESRI World Topo", "ESRI World Imagery"]).pack(side="left", padx=4)
        ttk.Button(bf, text="Apply", command=self._apply_basemap).pack(side="left", padx=4)

        zf = ttk.LabelFrame(r2, text="View")
        zf.pack(side="left")
        for txt, cmd in [("+", self.zoom_in), ("\u2212", self.zoom_out),
                         ("Fit domains", self.fit_view), ("Global", self.zoom_global)]:
            ttk.Button(zf, text=txt, command=cmd, width=10 if len(txt) > 1 else 3).pack(side="left", padx=2)

        self.info_label = tk.Label(pf, relief="sunken", anchor="w", bg="#d6e4f0", fg="#17365d",
                                   font=("Courier", 9), text="  Cursor: \u2014")
        self.info_label.pack(side="bottom", fill="x", pady=(2, 0))
        self.canvas_frame = ttk.Frame(pf, style="Card.TFrame")
        self.canvas_frame.pack(fill="both", expand=True)
        self.after(400, self.plot_domain_boxes)

    def setup_editor_tab(self):
        ef = ttk.Frame(self.tab_editor, padding=14)
        ef.pack(fill="both", expand=True)
        ef.rowconfigure(1, weight=1)
        ef.rowconfigure(3, weight=1)
        ef.columnconfigure(0, weight=1)
        t1 = ttk.Frame(ef)
        t1.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(t1, text="namelist.wps", font=("Arial", 11, "bold")).pack(side="left")
        ttk.Button(t1, text="Save namelist.wps", command=self.save_wps_file).pack(side="right")
        self.wps_text = tk.Text(ef, wrap="none", font=("Courier", 10), bg="#fafafa")
        self.wps_text.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        t2 = ttk.Frame(ef)
        t2.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(t2, text="namelist.input", font=("Arial", 11, "bold")).pack(side="left")
        ttk.Button(t2, text="Save namelist.input", command=self.save_nml_file).pack(side="right")
        self.nml_text = tk.Text(ef, wrap="none", font=("Courier", 10), bg="#fafafa")
        self.nml_text.grid(row=3, column=0, sticky="nsew")

    # ==================================================================
    # Small helpers
    # ==================================================================
    def _refresh_target_dropdown(self):
        values = [f"d{i + 1:02d}" for i in range(len(self.domain_vars))]
        try:
            self.target_domain_cb.configure(values=values)
        except (AttributeError, tk.TclError):
            return
        if self.target_domain_var.get() not in values:
            self.target_domain_var.set(values[0] if values else "d01")

    def _target_index(self):
        try:
            return int(self.target_domain_var.get().lstrip("dD")) - 1
        except (ValueError, AttributeError):
            return 0

    def _bounds(self, i):
        """(minlon, maxlon, minlat, maxlat) or None."""
        if i is None or i < 0 or i >= len(self.domain_vars):
            return None
        try:
            d = self.domain_vars[i]
            b = (float(d["minlon"].get()), float(d["maxlon"].get()),
                 float(d["minlat"].get()), float(d["maxlat"].get()))
        except (ValueError, TypeError):
            return None
        if not all(math.isfinite(x) for x in b) or b[0] >= b[1] or b[2] >= b[3]:
            return None
        return b

    def _set_bounds(self, i, b):
        d = self.domain_vars[i]
        d["minlon"].set(f"{b[0]:.4f}")
        d["maxlon"].set(f"{b[1]:.4f}")
        d["minlat"].set(f"{b[2]:.4f}")
        d["maxlat"].set(f"{b[3]:.4f}")
        self.geo_em_meta = {k: v for k, v in self.geo_em_meta.items() if k < i}

    def _parent_bounds(self, i):
        if i is None or i <= 0 or not self.constrain_to_parent_var.get():
            return None
        return self._bounds(i - 1)

    @staticmethod
    def _handle_points(b):
        x0, x1, y0, y1 = b
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return {'nw': (x0, y1), 'n': (cx, y1), 'ne': (x1, y1), 'e': (x1, cy),
                'se': (x1, y0), 's': (cx, y0), 'sw': (x0, y0), 'w': (x0, cy)}

    @staticmethod
    def _box_xy(b, n=20):
        x0, x1, y0, y1 = b
        lons, lats = np.linspace(x0, x1, n), np.linspace(y0, y1, n)
        xs = np.concatenate([lons, np.full(n, x1), lons[::-1], np.full(n, x0)])
        ys = np.concatenate([np.full(n, y0), lats, np.full(n, y1), lats[::-1]])
        return np.column_stack([xs, ys])

    def _select(self, idx, quiet=False):
        if idx is not None and (idx < 0 or idx >= len(self.domain_vars)):
            idx = None
        self.selected = idx
        self.sel_var.set(-1 if idx is None else idx)
        if idx is not None:
            self.target_domain_var.set(f"d{idx + 1:02d}")
            if not quiet:
                self.status.set(f"Selected d{idx + 1:02d}. Drag handles to resize, drag inside to move, "
                                "Delete to remove.")
        if self.canvas_mode == 'map' and self.ax is not None:
            self._update_artists()
            self.canvas.draw_idle()

    def on_draw_mode_toggle(self):
        if self.draw_mode_var.get():
            for i, d in enumerate(self.domain_vars):
                if not d["minlat"].get().strip():
                    self.target_domain_var.set(f"d{i + 1:02d}")
                    break
            self.status.set(f"Draw Mode ON. Target {self.target_domain_var.get()}: click-drag on the map. "
                            "Box stays editable via its handles.")
        else:
            self.status.set("Draw Mode off. Click a domain to select it.")
        self._apply_cursor(force=True)

    def _cancel_drawing(self):
        self.drawing = None
        if self.preview_poly is not None:
            try:
                self.preview_poly.remove()
            except Exception:
                pass
            self.preview_poly = None
        if self.canvas_mode == 'map' and self.ax is not None:
            self._update_artists()
            self.canvas.draw_idle()

    def load_example(self):
        self._push_undo()
        ex = [("-3.0627", "19.7253", "29.9502", "52.0498", "15"),
              ("-0.1085", "14.9522", "32.9059", "48.9577", "5"),
              ("1.8768", "6.9379", "34.8915", "40.8787", "1.667")]
        self._suspend_undo = True
        try:
            self._apply_rows([dict(zip(ROW_KEYS, (e[0], e[1], e[2], e[3], e[4]))) for e in ex])
            self.proj.set("Lambert Conformal")
            self.geo_em_meta = {}
            self._select(None, quiet=True)
        finally:
            self._suspend_undo = False
        self.status.set("Loaded 3-domain example.")
        self.plot_domain_boxes()

    # ==================================================================
    # geo_em loading
    # ==================================================================
    @staticmethod
    def _nc_attr(nc, names, default=None):
        attrs = nc.ncattrs()
        for n in names:
            if n in attrs:
                return nc.getncattr(n)
        return default

    def load_from_geo_em(self):
        try:
            import netCDF4
        except ImportError:
            messagebox.showerror("Missing Dependency", "Please install netCDF4:\npip install netCDF4")
            return
        directory = filedialog.askdirectory(title="Select folder with geo_em.d0X.nc files",
                                            initialdir=self.geogrid_dir.get() or str(Path.home()))
        if not directory:
            return
        files = sorted(Path(directory).expanduser().glob("geo_em.d*.nc"),
                       key=lambda p: int(re.sub(r"\D", "", p.stem.split('.')[-1]) or 0))
        if not files:
            messagebox.showerror("No files found", f"No geo_em.d*.nc files in:\n{directory}")
            return
        self._push_undo()
        rows, metas = [], {}
        for i, gf in enumerate(files[:9]):
            try:
                with netCDF4.Dataset(gf, 'r') as nc:
                    var_lat = 'XLAT_M' if 'XLAT_M' in nc.variables else 'XLAT'
                    var_lon = 'XLONG_M' if 'XLONG_M' in nc.variables else 'XLONG'
                    xlat = nc.variables[var_lat][:]
                    xlon = nc.variables[var_lon][:]
                    if xlat.ndim == 3:
                        xlat, xlon = xlat[0], xlon[0]
                    minlat, maxlat = float(xlat.min()), float(xlat.max())
                    minlon, maxlon = float(xlon.min()), float(xlon.max())
                    e_we = int(self._nc_attr(nc, ['WEST-EAST_GRID_DIMENSION'], xlon.shape[1] + 1))
                    e_sn = int(self._nc_attr(nc, ['SOUTH-NORTH_GRID_DIMENSION'], xlat.shape[0] + 1))
                    dx = float(self._nc_attr(nc, ['DX'], 30000.0))
                    dy = float(self._nc_attr(nc, ['DY'], dx))
                    code = self._nc_attr(nc, ['MAP_PROJ', 'MAPPROJ'], 1)
                    try:
                        mp = PROJ_CODES.get(int(code), 'lambert')
                    except (ValueError, TypeError):
                        txt = str(self._nc_attr(nc, ['MAP_PROJ_CHAR'], code)).lower()
                        mp = ('mercator' if 'merc' in txt else 'polar' if 'polar' in txt
                              else 'lat-lon' if 'lat' in txt else 'lambert')
                    cen_lat = float(self._nc_attr(nc, ['CEN_LAT'], (minlat + maxlat) / 2))
                    metas[i] = {
                        'e_we': e_we, 'e_sn': e_sn, 'dx': int(dx), 'dy': int(dy), 'mapproj': mp,
                        'truelat1': float(self._nc_attr(nc, ['TRUELAT1'], 0.0)),
                        'truelat2': float(self._nc_attr(nc, ['TRUELAT2'], 0.0)),
                        'stand_lon': float(self._nc_attr(nc, ['STAND_LON'], (minlon + maxlon) / 2)),
                        'cen_lat': cen_lat,
                        'cen_lon': float(self._nc_attr(nc, ['CEN_LON'], (minlon + maxlon) / 2)),
                        'moad_cen_lat': float(self._nc_attr(nc, ['MOAD_CEN_LAT'], cen_lat)),
                        'parent_id': int(self._nc_attr(nc, ['PARENT_ID'], max(1, i))),
                        'i_parent_start': int(self._nc_attr(nc, ['I_PARENT_START'], 1)),
                        'j_parent_start': int(self._nc_attr(nc, ['J_PARENT_START'], 1)),
                        'parent_grid_ratio': int(self._nc_attr(nc, ['PARENT_GRID_RATIO'],
                                                               1 if i == 0 else 3)),
                    }
                    rows.append({'minlat': f"{minlat:.4f}", 'maxlat': f"{maxlat:.4f}",
                                 'minlon': f"{minlon:.4f}", 'maxlon': f"{maxlon:.4f}",
                                 'resolution': f"{dx / 1000.0:.4g}"})
            except Exception as exc:
                messagebox.showerror(f"Error reading {gf.name}", str(exc))
                return
        self._suspend_undo = True
        try:
            self._apply_rows(rows)
            self.geo_em_meta = metas
            self.proj.set(PROJ_NAMES.get(metas[0]['mapproj'], 'Lambert Conformal'))
            self._select(None, quiet=True)
        finally:
            self._suspend_undo = False
        self.plot_domain_boxes()
        self.status.set(f"Loaded {len(rows)} domain(s) from geo_em. Generate will reproduce the exact grid.")

    # ==================================================================
    # Map plumbing
    # ==================================================================
    def _tf(self):
        return ccrs.PlateCarree()._as_mpl_transform(self.ax) if self.use_cartopy else self.ax.transData

    def _lonlat_to_data(self, lon, lat):
        if self.use_cartopy:
            x, y = self.ax.projection.transform_point(lon, lat, ccrs.PlateCarree())
            return x, y
        return lon, lat

    def _data_to_lonlat(self, x, y):
        if self.use_cartopy:
            lon, lat = ccrs.PlateCarree().transform_point(x, y, self.ax.projection)
            return lon, lat
        return x, y

    def _to_display(self, lon, lat):
        return self.ax.transData.transform(self._lonlat_to_data(lon, lat))

    def _event_to_lon_lat(self, event):
        if event.xdata is None or event.ydata is None:
            return None, None
        try:
            lon, lat = self._data_to_lonlat(event.xdata, event.ydata)
        except Exception:
            return None, None
        if math.isfinite(lon) and math.isfinite(lat):
            return float(lon), float(lat)
        return None, None

    def _view_lonlat(self):
        if self.use_cartopy:
            try:
                e = self.ax.get_extent(crs=ccrs.PlateCarree())
                return (e[0], e[1]), (e[2], e[3])
            except Exception:
                return (-180, 180), (-90, 90)
        return self.ax.get_xlim(), self.ax.get_ylim()

    def _set_view_lonlat(self, xl, yl):
        xl = (max(-180.0, xl[0]), min(180.0, xl[1]))
        yl = (max(-90.0, yl[0]), min(90.0, yl[1]))
        if self.use_cartopy:
            self.ax.set_extent([xl[0], xl[1], yl[0], yl[1]], crs=ccrs.PlateCarree())
        else:
            self.ax.set_xlim(xl)
            self.ax.set_ylim(yl)

    def _cartopy_projection(self):
        name = self.proj.get()
        m0 = self.geo_em_meta.get(0)
        b = self._bounds(0)
        if m0 is None and b is None:
            return ccrs.PlateCarree()
        if m0:
            lon0, lat0 = m0['stand_lon'], m0['cen_lat']
            t1, t2 = m0['truelat1'], m0['truelat2']
        else:
            lon0, lat0 = (b[0] + b[1]) / 2, (b[2] + b[3]) / 2
            t1 = max(-89, min(89, b[2] + (b[3] - b[2]) / 6))
            t2 = max(-89, min(89, b[3] - (b[3] - b[2]) / 6))
        if name == "Lambert Conformal":
            return ccrs.LambertConformal(central_longitude=lon0, central_latitude=lat0,
                                         standard_parallels=[t1, t2])
        if name == "Mercator":
            return ccrs.Mercator(central_longitude=lon0, latitude_true_scale=lat0)
        if name == "Polar Stereographic":
            return (ccrs.NorthPolarStereo(central_longitude=lon0) if lat0 >= 0
                    else ccrs.SouthPolarStereo(central_longitude=lon0))
        return ccrs.PlateCarree()

    # ------------------------------------------------------------------
    def plot_domain_boxes(self):
        """Full (re)build of the map figure; fits the view to the domains."""
        if not HAS_MPL:
            messagebox.showerror("Missing Dependency", "Please install matplotlib and numpy:\n"
                                                       "pip install matplotlib numpy")
            return
        try:
            for w in self.canvas_frame.winfo_children():
                w.destroy()
            self.fig = Figure(figsize=(9, 6), dpi=100)
            self.fig.subplots_adjust(left=0.07, right=0.98, bottom=0.08, top=0.93)
            self.canvas = FigureCanvasTkAgg(self.fig, master=self.canvas_frame)
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            if self.use_cartopy:
                self.ax = self.fig.add_subplot(111, projection=self._cartopy_projection())
                try:
                    self.ax.gridlines(draw_labels=False, linestyle="--", alpha=0.5, color='gray')
                except Exception:
                    pass
            else:
                self.ax = self.fig.add_subplot(111)
                self.ax.set_xlabel("Longitude (E)")
                self.ax.set_ylabel("Latitude (N)")
                self.ax.grid(True, linestyle="--", alpha=0.5)
            self.ax.set_title("Interactive Domain Wizard")
            self.canvas_mode = 'map'
            self._fig_proj = self.proj.get()
            self.domain_artists, self.legend, self.basemap_artist = {}, None, None
            self.preview_poly = None
            self.handle_line, = self.ax.plot([], [], linestyle='none', marker='s', markersize=9,
                                             markerfacecolor='white', markeredgecolor='black',
                                             markeredgewidth=1.5, zorder=9, transform=self._tf())
            self.fit_view(redraw=False)
            self.ax.set_autoscale_on(False)
            if self.use_cartopy:
                self._add_coastlines()
            if self.basemap_cache and self.basemap_var.get() == self.basemap_cache['service']:
                self._draw_basemap()
            self._update_artists()
            for ev, fn in (('button_press_event', self.on_press), ('motion_notify_event', self.on_motion),
                           ('button_release_event', self.on_release), ('scroll_event', self.on_scroll),
                           ('axes_leave_event', lambda e: self._update_info_bar())):
                self.canvas.mpl_connect(ev, fn)
            self.canvas.draw()
            self._update_info_bar()
            self._apply_cursor(force=True)
        except Exception as exc:
            messagebox.showerror("Preview Error", f"{type(exc).__name__}: {exc}")

    def _add_coastlines(self):
        for res in ('50m', '110m'):
            art = None
            try:
                art = self.ax.coastlines(resolution=res, color='gray', linewidth=0.6)
                self.canvas.draw()
                return
            except Exception:
                try:
                    if art is not None:
                        art.remove()
                except Exception:
                    pass
        self.status.set("Coastlines unavailable (Natural Earth data could not be loaded).")

    def update_overlays(self):
        """Cheap refresh: rebuild only if the figure is missing / projection changed."""
        if not HAS_MPL or not hasattr(self, 'canvas_frame'):
            return
        if self.canvas_mode != 'map' or self.ax is None or self._fig_proj != self.proj.get():
            self.plot_domain_boxes()
            return
        self._update_artists()
        self.canvas.draw_idle()
        self._update_info_bar()

    def _update_artists(self):
        """Update domain polygons / labels / handles in place."""
        if self.ax is None:
            return
        valid = {i: self._bounds(i) for i in range(len(self.domain_vars)) if self._bounds(i)}
        changed = set(valid) != set(self.domain_artists)
        for i in [k for k in self.domain_artists if k not in valid]:
            a = self.domain_artists.pop(i)
            for art in (a['poly'], a['label']):
                try:
                    art.remove()
                except Exception:
                    pass
        for i, b in valid.items():
            col = COLORS[i % len(COLORS)]
            a = self.domain_artists.get(i)
            if a is None:
                poly = Polygon(self._box_xy(b), closed=True, zorder=5, label=f"D{i + 1:02d}",
                               transform=self._tf())
                self.ax.add_patch(poly)
                lab = self.ax.text(b[0], b[3], f"D{i + 1:02d}", color=col, fontsize=11,
                                   fontweight='bold', va='top', zorder=6, transform=self._tf(),
                                   bbox=dict(facecolor='white', alpha=0.8, edgecolor='none', pad=1.5))
                a = self.domain_artists[i] = {'poly': poly, 'label': lab}
            else:
                a['poly'].set_xy(self._box_xy(b))
            sel = (i == self.selected)
            a['poly'].set_edgecolor(col)
            a['poly'].set_facecolor(to_rgba(col, 0.30 if sel else 0.14))
            a['poly'].set_linewidth(3.5 if sel else 2.0)
            a['label'].set_position((b[0] + (b[1] - b[0]) * 0.02, b[3] - (b[3] - b[2]) * 0.02))
        if changed:
            if self.legend is not None:
                try:
                    self.legend.remove()
                except Exception:
                    pass
                self.legend = None
            if valid:
                hs = [self.domain_artists[i]['poly'] for i in sorted(valid)]
                self.legend = self.ax.legend(handles=hs, loc="upper right")
        self._update_handles(valid.get(self.selected) if self.edit_mode_var.get() else None)

    def _update_handles(self, b):
        if self.handle_line is None:
            return
        if b:
            pts = self._handle_points(b).values()
            self.handle_line.set_data([p[0] for p in pts], [p[1] for p in pts])
            self.handle_line.set_visible(True)
        else:
            self.handle_line.set_data([], [])
            self.handle_line.set_visible(False)

    # ------------------------------------------------------------------
    # Basemap
    # ------------------------------------------------------------------
    def _fetch_esri_basemap(self, service, bbox):
        name = {"ESRI World Topo": "World_Topo_Map", "ESRI World Imagery": "World_Imagery"}.get(
            service, "World_Topo_Map")
        x0, y0, x1, y1 = bbox
        if x1 <= x0 or y1 <= y0:
            return None
        asp = (x1 - x0) / (y1 - y0)
        w, h = (1024, max(256, min(1024, int(1024 / asp)))) if asp > 1 else (
            max(256, min(1024, int(1024 * asp))), 1024)
        url = (f"https://server.arcgisonline.com/ArcGIS/rest/services/{name}/MapServer/export"
               f"?bbox={x0},{y0},{x1},{y1}&bboxSR=4326&imageSR=4326&size={w},{h}"
               f"&format=png&transparent=true&f=image")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "WRF-Domain-Wizard/2.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            import matplotlib.image as mpimg
            return mpimg.imread(io.BytesIO(data), format='png')
        except Exception as exc:
            self.status.set(f"Basemap error: {exc}")
            return None

    def _draw_basemap(self):
        c = self.basemap_cache
        if not c or self.ax is None:
            return
        if self.basemap_artist is not None:
            try:
                self.basemap_artist.remove()
            except Exception:
                pass
        xl, yl = self.ax.get_xlim(), self.ax.get_ylim()
        b = c['bbox']
        kw = dict(extent=[b[0], b[2], b[1], b[3]], origin='upper', zorder=0, alpha=0.85)
        if self.use_cartopy:
            kw['transform'] = ccrs.PlateCarree()
        else:
            kw['aspect'] = 'auto'
        self.basemap_artist = self.ax.imshow(c['img'], **kw)
        self.ax.set_xlim(xl)          # imshow must not change the view
        self.ax.set_ylim(yl)

    def _apply_basemap(self):
        if self.canvas_mode != 'map' or self.ax is None:
            messagebox.showinfo("Basemap", "Click 'Refresh Map' first.")
            return
        service = self.basemap_var.get()
        if service == "None":
            if self.basemap_artist is not None:
                try:
                    self.basemap_artist.remove()
                except Exception:
                    pass
                self.basemap_artist = None
            self.basemap_cache = None
            self.canvas.draw_idle()
            return
        xl, yl = self._view_lonlat()
        bbox = (max(-180.0, xl[0]), max(-90.0, yl[0]), min(180.0, xl[1]), min(90.0, yl[1]))
        self.status.set(f"Fetching {service}\u2026")
        self.update_idletasks()
        img = self._fetch_esri_basemap(service, bbox)
        if img is None:
            return
        self.basemap_cache = {'service': service, 'bbox': bbox, 'img': img}
        self._draw_basemap()
        self.canvas.draw_idle()
        self.status.set(f"{service} basemap loaded (cached; stays when you edit boxes).")

    # ------------------------------------------------------------------
    # View controls
    # ------------------------------------------------------------------
    def fit_view(self, redraw=True):
        if self.ax is None or self.canvas_mode != 'map':
            return
        bs = [self._bounds(i) for i in range(len(self.domain_vars))]
        bs = [b for b in bs if b]
        if not bs:
            if self.use_cartopy and not isinstance(self.ax.projection, ccrs.PlateCarree):
                self.ax.set_global()
            else:
                self._set_view_lonlat((-180, 180), (-90, 90))
        else:
            x0, x1 = min(b[0] for b in bs), max(b[1] for b in bs)
            y0, y1 = min(b[2] for b in bs), max(b[3] for b in bs)
            px, py = max(2.0, (x1 - x0) * 0.15), max(2.0, (y1 - y0) * 0.15)
            self._set_view_lonlat((x0 - px, x1 + px), (y0 - py, y1 + py))
        if redraw:
            self.canvas.draw_idle()
            self._update_info_bar()

    def _scale_view(self, f):
        if self.ax is None or self.canvas_mode != 'map':
            return
        xl, yl = self.ax.get_xlim(), self.ax.get_ylim()
        cx, cy = sum(xl) / 2, sum(yl) / 2
        self.ax.set_xlim(cx - (cx - xl[0]) * f, cx + (xl[1] - cx) * f)
        self.ax.set_ylim(cy - (cy - yl[0]) * f, cy + (yl[1] - cy) * f)
        self.canvas.draw_idle()
        self._update_info_bar()

    def zoom_in(self):
        self._scale_view(0.5)

    def zoom_out(self):
        self._scale_view(2.0)

    def zoom_global(self):
        if self.ax is None or self.canvas_mode != 'map':
            return
        if self.use_cartopy:
            self.ax.set_global()
        else:
            self._set_view_lonlat((-180, 180), (-90, 90))
        self.canvas.draw_idle()
        self._update_info_bar()

    def on_scroll(self, event):
        if self.canvas_mode != 'map' or event.inaxes is not self.ax or event.xdata is None:
            return
        f = 0.8 if event.button == 'up' else 1.25
        x, y = event.xdata, event.ydata
        xl, yl = self.ax.get_xlim(), self.ax.get_ylim()
        self.ax.set_xlim(x - (x - xl[0]) * f, x + (xl[1] - x) * f)
        self.ax.set_ylim(y - (y - yl[0]) * f, y + (yl[1] - y) * f)
        self.canvas.draw_idle()
        self._update_info_bar(*self._event_to_lon_lat(event))

    def _update_info_bar(self, lon=None, lat=None):
        if self.canvas_mode != 'map' or self.ax is None:
            return
        try:
            cx = sum(self.ax.get_xlim()) / 2
            cy = sum(self.ax.get_ylim()) / 2
            clon, clat = self._data_to_lonlat(cx, cy)
            xl, yl = self._view_lonlat()
            cur = (f"Cursor: {lat:+9.4f}N {lon:+10.4f}E" if lon is not None and lat is not None
                   else "Cursor:          --")
            sel = f"d{self.selected + 1:02d}" if self.selected is not None else "-"
            self.info_label.config(
                text=f"  {cur} | Centre: {clat:+8.3f}N {clon:+9.3f}E | "
                     f"View: {xl[1] - xl[0]:6.1f} x {yl[1] - yl[0]:5.1f} deg | Selected: {sel}")
        except Exception:
            pass

    def _apply_cursor(self, force=False, name=None):
        if name is None:
            name = "crosshair" if self.draw_mode_var.get() else (
                "hand2" if self.panning else "")
        if name == self._cursor and not force:
            return
        self._cursor = name
        try:
            self.canvas.get_tk_widget().configure(cursor=name)
        except Exception:
            pass

    # ==================================================================
    # Mouse interaction
    # ==================================================================
    def _hit_handle(self, event):
        b = self._bounds(self.selected)
        if not b:
            return None
        best, bd = None, HANDLE_PX
        for name, (lo, la) in self._handle_points(b).items():
            try:
                px, py = self._to_display(lo, la)
            except Exception:
                continue
            d = math.hypot(px - event.x, py - event.y)
            if d <= bd:
                best, bd = name, d
        return best

    def _domain_at(self, lon, lat):
        best, area = None, None
        for i in range(len(self.domain_vars)):
            b = self._bounds(i)
            if b and b[0] <= lon <= b[1] and b[2] <= lat <= b[3]:
                a = (b[1] - b[0]) * (b[3] - b[2])
                if area is None or a <= area:
                    best, area = i, a
        return best

    @staticmethod
    def _clamp(v, lo, hi):
        return max(lo, min(hi, v))

    def on_press(self, event):
        if self.canvas_mode != 'map' or event.inaxes is not self.ax:
            return
        lon, lat = self._event_to_lon_lat(event)
        if lon is None:
            return
        edit = self.edit_mode_var.get()

        if event.button == 3:                       # context menu
            idx = self._domain_at(lon, lat)
            if idx is not None:
                self._select(idx)
                self.ctx_menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
            return
        if event.button != 1:
            return

        if edit and self.selected is not None:      # 1. resize handle
            h = self._hit_handle(event)
            if h:
                self.interaction = {'kind': 'resize', 'idx': self.selected, 'handle': h,
                                    'snap': self._snapshot()}
                return
        if self.draw_mode_var.get():                # 2. rubber-band draw
            idx = self._target_index()
            if 0 <= idx < len(self.domain_vars):
                self.drawing = {'idx': idx, 'x0': lon, 'y0': lat, 'snap': self._snapshot()}
                self.preview_poly = Polygon(self._box_xy((lon, lon + 1e-6, lat, lat + 1e-6), 2),
                                            closed=True, fill=False, edgecolor='black',
                                            linestyle='--', linewidth=2, zorder=10,
                                            transform=self._tf())
                self.ax.add_patch(self.preview_poly)
                self.canvas.draw_idle()
                return
        if edit:                                    # 3. select + move
            idx = self._domain_at(lon, lat)
            if idx is not None:
                self._select(idx)
                self.interaction = {'kind': 'move', 'idx': idx, 'x0': lon, 'y0': lat,
                                    'b0': self._bounds(idx), 'snap': self._snapshot()}
                return
            self._select(None, quiet=True)
        self.panning = True                         # 4. pan
        bbox = self.ax.get_window_extent()
        self.pan_state = {'px': event.x, 'py': event.y, 'xl': self.ax.get_xlim(),
                          'yl': self.ax.get_ylim(), 'w': max(1.0, bbox.width),
                          'h': max(1.0, bbox.height)}
        self._apply_cursor(name="hand2")

    def _constrained_box(self, idx, b):
        """Clamp box edges into the parent box (if enabled) and into the globe."""
        x0, x1, y0, y1 = b
        pb = self._parent_bounds(idx)
        lo_x, hi_x, lo_y, hi_y = (pb if pb else (-180.0, 180.0, -90.0, 90.0))
        return (self._clamp(x0, lo_x, hi_x), self._clamp(x1, lo_x, hi_x),
                self._clamp(y0, lo_y, hi_y), self._clamp(y1, lo_y, hi_y))

    def on_motion(self, event):
        if self.canvas_mode != 'map' or self.ax is None:
            return
        lon, lat = self._event_to_lon_lat(event)
        if lon is not None:
            self._update_info_bar(lon, lat)

        if self.panning and self.pan_state:
            s = self.pan_state
            dx = -(event.x - s['px']) / s['w'] * (s['xl'][1] - s['xl'][0])
            dy = -(event.y - s['py']) / s['h'] * (s['yl'][1] - s['yl'][0])
            self.ax.set_xlim(s['xl'][0] + dx, s['xl'][1] + dx)
            self.ax.set_ylim(s['yl'][0] + dy, s['yl'][1] + dy)
            self.canvas.draw_idle()
            return
        if lon is None:
            return

        if self.drawing:
            d = self.drawing
            b = (min(d['x0'], lon), max(d['x0'], lon), min(d['y0'], lat), max(d['y0'], lat))
            b = self._constrained_box(d['idx'], b)
            if b[1] > b[0] and b[3] > b[2]:
                self.preview_poly.set_xy(self._box_xy(b))
                self._update_handles(b)
                self.canvas.draw_idle()
            return

        it = self.interaction
        if it:
            i = it['idx']
            if it['kind'] == 'move':
                x0, x1, y0, y1 = it['b0']
                dx, dy = lon - it['x0'], lat - it['y0']
                pb = self._parent_bounds(i)
                lo_x, hi_x, lo_y, hi_y = pb if pb else (-180.0, 180.0, -90.0, 90.0)
                dx = self._clamp(dx, lo_x - x0, hi_x - x1)
                dy = self._clamp(dy, lo_y - y0, hi_y - y1)
                nb = (x0 + dx, x1 + dx, y0 + dy, y1 + dy)
            else:
                x0, x1, y0, y1 = self._bounds(i) or (0, 0, 0, 0)
                h = it['handle']
                cb = self._constrained_box(i, (lon, lon, lat, lat))
                cx, cy = cb[0], cb[2]
                if 'w' in h:
                    x0 = min(cx, x1 - MIN_SPAN)
                if 'e' in h:
                    x1 = max(cx, x0 + MIN_SPAN)
                if 's' in h:
                    y0 = min(cy, y1 - MIN_SPAN)
                if 'n' in h:
                    y1 = max(cy, y0 + MIN_SPAN)
                nb = (x0, x1, y0, y1)
            self._set_bounds(i, nb)
            self._update_artists()
            self.canvas.draw_idle()
            return

        # hover cursor
        name = None
        if self.edit_mode_var.get():
            h = self._hit_handle(event) if self.selected is not None else None
            if h:
                name = HANDLE_CURSORS[h]
            elif not self.draw_mode_var.get() and self._domain_at(lon, lat) is not None:
                name = "fleur"
        self._apply_cursor(name=name)

    def on_release(self, event):
        if self.panning:
            self.panning = False
            self.pan_state = None
            self._apply_cursor(force=True)
            self._update_info_bar()
            return

        if self.drawing:
            d, self.drawing = self.drawing, None
            poly, self.preview_poly = self.preview_poly, None
            try:
                xy = poly.get_xy()
                b = (float(xy[:, 0].min()), float(xy[:, 0].max()),
                     float(xy[:, 1].min()), float(xy[:, 1].max()))
                poly.remove()
            except Exception:
                b = None
            idx = d['idx']
            if b and (b[1] - b[0]) > 0.05 and (b[3] - b[2]) > 0.05:
                self._push_snap(d['snap'])
                self._set_bounds(idx, b)
                self._select(idx, quiet=True)
                self.status.set(
                    f"d{idx + 1:02d} drawn: lon [{b[0]:.3f}, {b[1]:.3f}] lat [{b[2]:.3f}, {b[3]:.3f}]. "
                    "Drag the handles to fine-tune.")
                if self.draw_mode_var.get():
                    for j in range(idx + 1, len(self.domain_vars)):
                        if not self.domain_vars[j]["minlat"].get().strip():
                            self.target_domain_var.set(f"d{j + 1:02d}")
                            self.status.set(self.status.get() + f"  Next target: d{j + 1:02d}.")
                            break
            self._update_artists()
            self.canvas.draw_idle()
            return

        if self.interaction:
            it, self.interaction = self.interaction, None
            cur = self._snapshot()
            if it['snap']['domains'] != cur['domains']:
                self._push_snap(it['snap'])
                self.status.set(f"d{it['idx'] + 1:02d} {'moved' if it['kind'] == 'move' else 'resized'}.")
            self._update_artists()
            self.canvas.draw_idle()

    # ==================================================================
    # Terrain plot
    # ==================================================================
    def plot_terrain(self):
        try:
            gdir = Path(self.geogrid_dir.get()).expanduser()
            gf = gdir / "geo_em.d01.nc"
            if not gf.exists():
                sel = filedialog.askopenfilename(title="Select geo_em file", initialdir=str(gdir),
                                                 filetypes=[("NetCDF files", "*.nc")])
                if not sel:
                    return
                gf = Path(sel)
            import netCDF4
            with netCDF4.Dataset(gf, 'r') as nc:
                hgt = nc.variables['HGT_M'][:]
                xlat = nc.variables['XLAT_M'][:]
                xlon = nc.variables['XLONG_M'][:]
            if hgt.ndim == 3:
                hgt, xlat, xlon = hgt[0], xlat[0], xlon[0]
            for w in self.canvas_frame.winfo_children():
                w.destroy()
            self.fig = Figure(figsize=(9, 6), dpi=100)
            ax = self.fig.add_subplot(111)
            cf = ax.pcolormesh(xlon, xlat, hgt, cmap='terrain', shading='auto')
            self.fig.colorbar(cf, ax=ax, label='Terrain Height (m)')
            ax.set_title(f"Terrain Height (HGT_M) -- {gf.name}   [Refresh Map to go back]")
            ax.set_xlabel("Longitude (E)")
            ax.set_ylabel("Latitude (N)")
            self.canvas = FigureCanvasTkAgg(self.fig, master=self.canvas_frame)
            self.canvas.draw()
            self.canvas.get_tk_widget().pack(fill="both", expand=True)
            self.canvas_mode, self.ax = 'terrain', None
            self.domain_artists, self.handle_line, self.legend = {}, None, None
            self.status.set(f"Rendered terrain from {gf.name}")
        except ImportError:
            messagebox.showerror("Missing Dependency", "Please install netCDF4:\npip install netCDF4")
        except Exception as exc:
            messagebox.showerror("Plotting Error", str(exc))

    # ==================================================================
    # Save editor files
    # ==================================================================
    def _save_text(self, widget, name):
        try:
            outdir = Path(self.outdir.get()).expanduser()
            outdir.mkdir(parents=True, exist_ok=True)
            p = outdir / name
            p.write_text(widget.get("1.0", tk.END))
            messagebox.showinfo("Saved", f"Saved {p}")
        except Exception as exc:
            messagebox.showerror("Error", str(exc))

    def save_wps_file(self):
        self._save_text(self.wps_text, "namelist.wps")

    def save_nml_file(self):
        self._save_text(self.nml_text, "namelist.input")

    # ==================================================================
    # Generate namelists
    # ==================================================================
    def generate(self):
        try:
            maxdom = len(self.domain_vars)
            v = {k: x.get().strip() for k, x in self.vars.items()}
            input_interval = int(v["input_interval"])
            timestep = int(v["time_step"])
            history_interval = int(v["history_interval"])
            start = parse_date(v["start"], "Simulation start")
            end = parse_date(v["end"], "Simulation end")
            if end <= start:
                raise ValueError("Simulation end must be later than simulation start.")
            if min(input_interval, timestep, history_interval) <= 0:
                raise ValueError("Input interval, timestep and history interval must be positive.")

            doms = []
            for i in range(maxdom):
                d = self.domain_vars[i]
                try:
                    a, b = float(d["minlat"].get()), float(d["maxlat"].get())
                    c, e = float(d["minlon"].get()), float(d["maxlon"].get())
                    res = float(d["resolution"].get())
                except ValueError:
                    raise ValueError(f"Domain d{i + 1:02d}: extents/resolution are empty or not numeric. "
                                     "Draw the domain or type its extents.")
                if not (-90 <= a < b <= 90):
                    raise ValueError(f"Domain d{i + 1:02d}: need -90 <= minlat < maxlat <= 90.")
                if not (-180 <= c < e <= 180):
                    raise ValueError(f"Domain d{i + 1:02d}: need -180 <= minlon < maxlon <= 180.")
                if not (0 < res <= 1000):
                    raise ValueError(f"Domain d{i + 1:02d}: grid spacing must be > 0 and <= 1000 km.")
                doms.append({"minlat": a, "maxlat": b, "minlon": c, "maxlon": e, "resolution": res})

            d1 = doms[0]
            if 0 in self.geo_em_meta:
                m0 = self.geo_em_meta[0]
                map_proj = m0['mapproj']
                ref_lat, ref_lon, stand_lon = m0['cen_lat'], m0['cen_lon'], m0['stand_lon']
                midlat = ref_lat
                truelat1, truelat2 = m0['truelat1'], m0['truelat2']
                dx_base, dy_base = m0['dx'], m0['dy']
            else:
                midlat = (d1["minlat"] + d1["maxlat"]) / 2
                ref_lat = midlat
                ref_lon = stand_lon = (d1["minlon"] + d1["maxlon"]) / 2
                pn = self.proj.get()
                if pn == "Lambert Conformal":
                    map_proj = "lambert"
                    truelat1 = max(-89, min(89, d1["minlat"] + (d1["maxlat"] - d1["minlat"]) / 6))
                    truelat2 = max(-89, min(89, d1["maxlat"] - (d1["maxlat"] - d1["minlat"]) / 6))
                elif pn == "Mercator":
                    map_proj, truelat1, truelat2 = "mercator", max(-89, min(89, midlat)), 0.0
                elif pn == "Polar Stereographic":
                    map_proj, truelat1, truelat2 = "polar", (60.0 if midlat >= 0 else -60.0), 0.0
                else:
                    map_proj, truelat1, truelat2 = "lat-lon", 0.0, 0.0
                dx_base = dy_base = int(round(d1["resolution"] * 1000))

            ewes, esns, dxs, dys = [], [], [], []
            pids, ratios, istarts, jstarts, trats = [], [], [], [], []
            gids, verts, hists, inputs = [], [], [], []
            mps, lws, sws, pbls, sfclays, surfs, cus, levels = [], [], [], [], [], [], [], []

            parent_sw_lon, parent_sw_lat = ref_lon, midlat
            parent_ref_lat, parent_dx_m = midlat, float(dx_base)
            e_we0 = e_sn0 = None
            if 0 not in self.geo_em_meta:
                wkm = 111.32 * (d1["maxlon"] - d1["minlon"]) * math.cos(math.radians(midlat))
                hkm = 111.32 * (d1["maxlat"] - d1["minlat"])
                e_we0 = even_at_least(wkm / d1["resolution"] + 1)
                e_sn0 = even_at_least(hkm / d1["resolution"] + 1)
                parent_sw_lon = ref_lon - (e_we0 / 2.0) * (dx_base / (111.32 * 1000 * math.cos(math.radians(midlat))))
                parent_sw_lat = midlat - (e_sn0 / 2.0) * (dx_base / (111.32 * 1000))

            for i in range(maxdom):
                di = doms[i]
                if i in self.geo_em_meta:
                    m = self.geo_em_meta[i]
                    ewes.append(str(m['e_we']))
                    esns.append(str(m['e_sn']))
                    dxs.append(str(m['dx']))
                    dys.append(str(m['dy']))
                    pids.append(str(m['parent_id']))
                    ratios.append(str(m['parent_grid_ratio']))
                    istarts.append(str(m['i_parent_start']))
                    jstarts.append(str(m['j_parent_start']))
                    trats.append(str(m['parent_grid_ratio']))
                    parent_sw_lon, parent_sw_lat = di["minlon"], di["minlat"]
                    parent_ref_lat = (di["minlat"] + di["maxlat"]) / 2
                    parent_dx_m = float(m['dx'])
                elif i == 0:
                    ewes.append(str(e_we0))
                    esns.append(str(e_sn0))
                    dxs.append(str(dx_base))
                    dys.append(str(dy_base))
                    pids.append("1")
                    ratios.append("1")
                    istarts.append("1")
                    jstarts.append("1")
                    trats.append("1")
                else:
                    ratio = int(round(parent_dx_m / (di["resolution"] * 1000)))
                    if ratio < 2:
                        ratio = 3
                    dx_i = int(round(di["resolution"] * 1000))
                    cml = (di["minlat"] + di["maxlat"]) / 2
                    cwk = 111.32 * (di["maxlon"] - di["minlon"]) * math.cos(math.radians(cml))
                    chk = 111.32 * (di["maxlat"] - di["minlat"])
                    e_we_i = align_dimension(cwk / di["resolution"] + 1, ratio=ratio)
                    e_sn_i = align_dimension(chk / di["resolution"] + 1, ratio=ratio)
                    dxok = 111.32 * (di["minlon"] - parent_sw_lon) * math.cos(math.radians(parent_ref_lat))
                    dyok = 111.32 * (di["minlat"] - parent_sw_lat)
                    ist = max(1, 1 + int(round((dxok * 1000) / parent_dx_m)))
                    jst = max(1, 1 + int(round((dyok * 1000) / parent_dx_m)))
                    ewes.append(str(e_we_i))
                    esns.append(str(e_sn_i))
                    dxs.append(str(dx_i))
                    dys.append(str(dx_i))
                    pids.append(str(i))
                    ratios.append(str(ratio))
                    istarts.append(str(ist))
                    jstarts.append(str(jst))
                    trats.append(str(ratio))
                    parent_sw_lon, parent_sw_lat = di["minlon"], di["minlat"]
                    parent_ref_lat, parent_dx_m = cml, float(dx_i)
                gids.append(str(i + 1))
                verts.append("45")
                hists.append(str(history_interval))
                inputs.append(".true.")
                mps.append("8")
                lws.append("4")
                sws.append("4")
                pbls.append("1")
                sfclays.append("1")
                surfs.append("2")
                cus.append("0" if i > 0 else "1")
                levels.append("'default'")

            warnings = []
            for i in range(1, maxdom):
                p = int(pids[i]) - 1
                r, ist, jst = int(ratios[i]), int(istarts[i]), int(jstarts[i])
                we, sn = int(ewes[i]), int(esns[i])
                if (we - 1) % r or (sn - 1) % r:
                    warnings.append(f"d{i + 1:02d}: (e_we-1) and (e_sn-1) must be divisible by ratio {r}.")
                if 0 <= p < maxdom and (ist + (we - 1) / r > int(ewes[p]) or
                                        jst + (sn - 1) / r > int(esns[p])):
                    warnings.append(f"d{i + 1:02d} does not fit inside d{p + 1:02d}; "
                                    "shrink or move it inside its parent.")

            dom = {
                "parent_ids": ", ".join(pids), "ratios": ", ".join(ratios),
                "i_starts": ", ".join(istarts), "j_starts": ", ".join(jstarts),
                "ewes": ", ".join(ewes), "esns": ", ".join(esns),
                "dxs": ", ".join(dxs), "dys": ", ".join(dys),
                "time_ratios": ", ".join(trats), "grid_ids": ", ".join(gids),
                "verts": ", ".join(verts), "history": ", ".join(hists),
                "inputs": ", ".join(inputs), "mp": ", ".join(mps), "lw": ", ".join(lws),
                "sw": ", ".join(sws), "pbl": ", ".join(pbls), "sfclay": ", ".join(sfclays),
                "surface": ", ".join(surfs), "cu": ", ".join(cus),
                "feedback": "0" if maxdom == 1 else "1",
                "nested": ".false." if maxdom == 1 else ".true.",
                "levels": ", ".join(levels),
            }

            outdir = Path(self.outdir.get()).expanduser()
            outdir.mkdir(parents=True, exist_ok=True)
            esc = lambda s: str(Path(s.strip()).expanduser()).replace("'", "\\'")
            geog, geogrid_dir = esc(self.geog.get()), esc(self.geogrid_dir.get())
            ungrib_prefix, metgrid_dir = esc(self.ungrib_prefix.get()), esc(self.metgrid_dir.get())
            Path(geogrid_dir).mkdir(parents=True, exist_ok=True)
            Path(ungrib_prefix).parent.mkdir(parents=True, exist_ok=True)
            Path(metgrid_dir).mkdir(parents=True, exist_ok=True)
            sd = ", ".join([f"'{v['start']}'"] * maxdom)
            ed = ", ".join([f"'{v['end']}'"] * maxdom)

            wps = f"""&share
 wrf_core = 'ARW',
 max_dom = {maxdom},
 start_date = {sd},
 end_date   = {ed},
 interval_seconds = {input_interval * 3600},
 io_form_geogrid = 2,
 opt_output_from_geogrid_path = '{geogrid_dir}/',
/

&geogrid
 parent_id         = {dom['parent_ids']},
 parent_grid_ratio = {dom['ratios']},
 i_parent_start    = {dom['i_starts']},
 j_parent_start    = {dom['j_starts']},
 e_we              = {dom['ewes']},
 e_sn              = {dom['esns']},
 geog_data_res     = {dom['levels']},
 dx = {dx_base},
 dy = {dy_base},
 map_proj = '{map_proj}',
 ref_lat = {ref_lat:.5f},
 ref_lon = {ref_lon:.5f},
 truelat1 = {truelat1:.5f},
 truelat2 = {truelat2:.5f},
 stand_lon = {stand_lon:.5f},
 geog_data_path = '{geog}',
/

&ungrib
 out_format = 'WPS',
 prefix = '{ungrib_prefix}',
/

&metgrid
 fg_name = '{ungrib_prefix}',
 io_form_metgrid = 2,
 opt_output_from_metgrid_path = '{metgrid_dir}/',
/
"""
            total = int((end - start).total_seconds())
            days, rem = divmod(total, 86400)
            hours, rem = divmod(rem, 3600)
            minutes, seconds = divmod(rem, 60)
            sv = (start.year, start.month, start.day, start.hour, start.minute, start.second)
            ev = (end.year, end.month, end.day, end.hour, end.minute, end.second)
            rep = lambda x: ", ".join([str(x)] * maxdom)

            model_flags = {k: var.get().strip() for k, var in self.model_values.items()}
            chemistry_enabled = self.model_flags["chemistry"].get()
            hydro_enabled = self.model_flags["hydro"].get()
            fire_enabled = self.model_flags["fire"].get()
            extra_physics = []
            extra_chem = []
            extra_fire = []
            chem_keys = ("chem_opt", "chem_in_opt", "bio_emiss_opt", "emiss_opt",
                         "emiss_inpt_opt", "dust_opt", "seas_opt", "phot_opt",
                         "gas_drydep_opt", "aer_drydep_opt", "aer_rad_feedback")
            fire_keys = ("ifire", "fire_fuel_read", "fire_fuel_cat", "sr_x", "sr_y",
                         "fire_wind_height", "fire_num_ignitions", "fire_ignition_start_x1",
                         "fire_ignition_end_x1", "fire_ignition_start_y1", "fire_ignition_end_y1",
                         "fire_ignition_start_time1", "fire_ignition_end_time1", "fire_ignition_radius1")
            if chemistry_enabled:
                for key in chem_keys:
                    if not model_flags[key]:
                        raise ValueError(f"{key} cannot be empty when WRF-Chem is enabled.")
                    extra_chem.append(f" {key} = {model_flags[key]},")
            if fire_enabled:
                for key in fire_keys:
                    if not model_flags[key]:
                        raise ValueError(f"{key} cannot be empty when fire options are enabled.")
                    extra_fire.append(f" {key} = {model_flags[key]},")
            if model_flags["sf_urban_physics"]:
                extra_physics.append(f" sf_urban_physics = {model_flags['sf_urban_physics']},")

            hydro_note = (
                "! WRF-Hydro additionally requires hydro.namelist and namelist.hrldas.\n"
                "! Generated files are templates; verify settings for your installed release.\n"
                if hydro_enabled else ""
            )
            wps = "! Chemistry, WRF-Hydro and fire runtime controls are not WPS namelist options.\n" + wps
            nml = f"""! Generated by WRF/WPS Domain Wizard
{hydro_note}&time_control
 run_days = {days},
 run_hours = {hours},
 run_minutes = {minutes},
 run_seconds = {seconds},
 debug_level = 0,
 start_year = {rep(sv[0])},
 start_month = {rep(sv[1])},
 start_day = {rep(sv[2])},
 start_hour = {rep(sv[3])},
 start_minute = {rep(sv[4])},
 start_second = {rep(sv[5])},
 end_year = {rep(ev[0])},
 end_month = {rep(ev[1])},
 end_day = {rep(ev[2])},
 end_hour = {rep(ev[3])},
 end_minute = {rep(ev[4])},
 end_second = {rep(ev[5])},
 interval_seconds = {input_interval * 3600},
 input_from_file = {dom['inputs']},
 history_interval = {dom['history']},
 frames_per_outfile = 1,
 restart = .false.,
 restart_interval = 1440,
 io_form_history = 2,
 io_form_restart = 2,
 io_form_input = 2,
 io_form_boundary = 2,
/

&domains
 time_step = {timestep},
 time_step_fract_num = 0,
 time_step_fract_den = 1,
 max_dom = {maxdom},
 e_we = {dom['ewes']},
 e_sn = {dom['esns']},
 e_vert = {dom['verts']},
 p_top_requested = 5000,
 num_metgrid_levels = 34,
 num_metgrid_soil_levels = 4,
 dx = {dom['dxs']},
 dy = {dom['dys']},
 grid_id = {dom['grid_ids']},
 parent_id = {dom['parent_ids']},
 i_parent_start = {dom['i_starts']},
 j_parent_start = {dom['j_starts']},
 parent_grid_ratio = {dom['ratios']},
 parent_time_step_ratio = {dom['time_ratios']},
 feedback = {dom['feedback']},
 smooth_option = 0,
/

&physics
 mp_physics = {dom['mp']},
 ra_lw_physics = {dom['lw']},
 ra_sw_physics = {dom['sw']},
 bl_pbl_physics = {dom['pbl']},
 sf_sfclay_physics = {dom['sfclay']},
 sf_surface_physics = {dom['surface']},
 cu_physics = {dom['cu']},
{chr(10).join(extra_physics)}
/

{("&chem\n" + chr(10).join(extra_chem) + "\n/") if extra_chem else ""}
{("&fire\n" + chr(10).join(extra_fire) + "\n/") if extra_fire else ""}
&dynamics
 hybrid_opt = 2,
 w_damping = 1,
 diff_opt = 1,
 km_opt = 4,
 diff_6th_opt = 0,
 diff_6th_factor = 0.12,
 base_temp = 290.,
 damp_opt = 3,
 zdamp = 5000.,
 dampcoef = 0.2,
 khdif = 0,
 kvdif = 0,
 non_hydrostatic = .true.,
 moist_adv_opt = 1,
 scalar_adv_opt = 1,
/

&bdy_control
 spec_bdy_width = 5,
 spec_zone = 1,
 relax_zone = 4,
 specified = .true.,
 nested = {dom['nested']},
/

&namelist_quilt
 nio_tasks_per_group = 0,
 nio_groups = 1,
/
"""
            (outdir / "namelist.wps").write_text(wps)
            (outdir / "namelist.input").write_text(nml)
            generated_extra_files = []
            if hydro_enabled:
                hydro_namelist = f"""! WRF-Hydro namelist template generated by WRF/WPS Domain Wizard
! IMPORTANT: Variable names and required sections differ by WRF-Hydro release.
! Compare every setting with the hydro.namelist supplied with your installed source/build.
&hydro_nlist
! Commonly used routing/runtime controls; confirm names and values for your release.
 rstrt_file = '' ,
 rst_dt = 60,
 out_dt = 60,
 outdir = './',
 channel_option = 1,
 route_lsm = 1,
 route_kinematic = 1,
 udt = 1,
 dx = 1000,
 aggfact = 1,
 geo_finegrid_flnm = '',
/
"""
                hrldas_namelist = f"""! HRLDAS namelist template generated by WRF/WPS Domain Wizard
! IMPORTANT: Use the namelist.hrldas template distributed with your WRF-Hydro release.
! Required options and section names are release/build specific; this is a starter template.
&NOAHLSM_OFFLINE
! Set start/end dates, forcing paths, output paths, and timestep for your experiment.
HRLDAS_CONSTANTS = './',
INDIR = './FORCING',
OUTDIR = './',
START_YEAR = {sv[0]},
START_MONTH = {sv[1]},
START_DAY = {sv[2]},
START_HOUR = {sv[3]},
KHOUR = {max(1, int(total // 3600))},
NOAH_TIMESTEP = 3600,
/ 
"""
                (outdir / "hydro.namelist").write_text(hydro_namelist)
                (outdir / "namelist.hrldas").write_text(hrldas_namelist)
                generated_extra_files.extend(["hydro.namelist", "namelist.hrldas"])
            self.wps_text.delete("1.0", tk.END)
            self.wps_text.insert("1.0", wps)
            self.nml_text.delete("1.0", tk.END)
            self.nml_text.insert("1.0", nml)
            self.status.set(f"SUCCESS -- namelists written to {outdir}: namelist.wps, namelist.input"
                            + (f", {', '.join(generated_extra_files)}" if generated_extra_files else "")
                            + (f" (exact grid reused for {len(self.geo_em_meta)} domain(s))"
                               if self.geo_em_meta else "")
                            + (f"\nWARNINGS: {' '.join(warnings)}" if warnings else ""))
            if warnings:
                messagebox.showwarning("Namelists generated with warnings", "\n".join(warnings))
            else:
                messagebox.showinfo("Namelists generated",
                                    f"Created namelist.wps and namelist.input in:\n{outdir}")
        except Exception as exc:
            messagebox.showerror("Cannot generate namelists", str(exc))
            self.status.set(f"ERROR: {exc}")


if __name__ == "__main__":
    app = DomainGUI()
    app.mainloop()