#!/usr/bin/env perl
# =============================================================================
#  wrfall_install.pl
#
#  A perl installer / updater for WRF + WPS (ARW, Chem, Hydro, Fire) that needs
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
#  Perl 5.14+, core modules only (cdsapi is needed only for ERA5 data).
#  This is a full 1:1 port of wrfall_install.py v1.4.2.  Implementation notes:
#    * the pure-Python fetcher is replaced by an HTTP::Tiny-based one (core
#      module) with the same parallel-range/resume behaviour -- for https it
#      needs IO::Socket::SSL, so wget/curl remain the preferred "auto" tools;
#    * the threaded parallel download workers are fork()ed processes instead
#      of threads, communicating through the same <file>.pNNN markers;
#    * cdsapi (Python) is still invoked through python3 for ERA5 downloads.
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
#     ./wrfall_install.pl --install -arw
#     ./wrfall_install.pl --install -fire            (WRF-Fire, real-data runs)
#     ./wrfall_install.pl --install -fireideal       (WRF-Fire ideal cases, em_fire)
#     ./wrfall_install.pl --install list
#     ./wrfall_install.pl --update
#     ./wrfall_install.pl --addpack domwiz,scripts --addgeog chem
#     ./wrfall_install.pl --mkdomain ethiowrf
#     ./wrfall_install.pl --mkdomain firedom -fire   (adds &fire, sr_x/sr_y, subgrid_ratio)
#     ./wrfall_install.pl --addgeog fire             (landfire/ + highres_elev/ folders)
#     ./wrfall_install.pl --getdata gfs --domain ~/Build_WRF/ethiowrf --date 20261016 --end 72
#     ./wrfall_install.pl --run 20261016 00 72 --domain ~/Build_WRF/ethiowrf
# =============================================================================
use strict;
use warnings;

$| = 1;
binmode(STDOUT, ":encoding(UTF-8)");
binmode(STDERR, ":encoding(UTF-8)");

use Cwd qw(getcwd);
use File::Basename qw(basename dirname);
use File::Copy qw(copy move);
use File::Path qw(make_path rmtree);
use List::Util qw(min max);
use POSIX ();
use File::Find ();
use Fcntl qw(O_CREAT O_EXCL O_RDWR);
use Sys::Hostname qw(hostname);
use Time::HiRes qw(time sleep);
use Time::Local qw(timegm);

my $HAVE_HTTP = eval { require HTTP::Tiny; 1 } ? 1 : 0;
my $HAVE_SSL  = eval { require IO::Socket::SSL; 1 } ? 1 : 0;
my $HAVE_JSON = eval { require JSON::PP; 1 } ? 1 : 0;

my $VERSION = "1.4.2";
my $EXE = basename($0) || "wrfall_install.pl";
my $UA = "Wget/1.21.4";   # some CDNs (e.g. Cloudflare in front of mpich.org) throttle unknown clients

my $DEFAULT_RELEASE = "4.6.0";
my $WRF_TAGS_API = "https://api.github.com/repos/wrf-model/WRF/releases?per_page=40";
my $WRF_URL = "https://github.com/wrf-model/WRF/releases/download/v%s/v%s.tar.gz";
my $WPS_URL = "https://github.com/wrf-model/WPS/archive/v%s.tar.gz";
my $HYDRO_URL = "https://github.com/NCAR/wrf_hydro_nwm_public/archive/refs/tags/v5.3.0.tar.gz";
my $GEOG_BASE = "https://www2.mmm.ucar.edu/wrf/src/wps_files";
my $UTIL_RAW = "https://raw.githubusercontent.com/goikelem/Automation-Utility-Scripts/main/%s";
my $DOMWIZ_REPO = "https://github.com/goikelem/WRF-domain-Wizard";
my $UTIL_REPO = "https://github.com/goikelem/Automation-Utility-Scripts";

my @BUILD_TYPES = qw(arw chem hydro fire fireideal);
my @ALL_TYPES = qw(arw chem hydro fire);      # -all (fireideal is opt-in)
my @NO_WPS_TYPES = qw(fireideal);             # types that build WRF only
my %TYPE_LABELS = (arw => "ARW", chem => "Chem", hydro => "Hydro",
                   fire => "Fire", fireideal => "FireIdeal");

# ----------------------------------------------------------------------------
#  WRF-Fire (WRF Users Guide, chapter "WRF-Fire")
#    -fire       : standard WRF-ARW source, ./compile em_real, plus WPS prepared
#                  for the fire grid (NFUEL_CAT / ZSF with subgrid=yes).
#    -fireideal  : ./compile em_fire (hill_simple, two_fires), WRF only.
#  No special environment variable is needed; the fire module (ifire=2, level
#  set method) is part of every WRF-ARW build and is switched on in namelist.input.
# ----------------------------------------------------------------------------
my $FIRE_SR = 10;                 # sr_x / sr_y / subgrid_ratio_x / subgrid_ratio_y (must be even)
my @FIRE_GEOG_DIRS = qw(landfire highres_elev);
my $_SEP = "=" x 32;
my $FIRE_GEOGRID_ENTRIES = join("",
    "name=NFUEL_CAT\n",
    "        priority=1\n",
    "        dest_type=categorical\n",
    "        dominant_only=NFUEL_CAT\n",
    "        z_dim_name=fuel_cat\n",
    "        halt_on_missing=yes\n",
    "        interp_option=default:nearest_neighbor+average_16pt+search\n",
    "        rel_path=default:landfire/\n",
    "        subgrid=yes\n", $_SEP, "\n",
    "name=ZSF\n",
    "        priority=1\n",
    "        dest_type=continuous\n",
    "        halt_on_missing=yes\n",
    "        interp_option=default:four_pt\n",
    "        rel_path=default:highres_elev/\n",
    "        subgrid=yes\n", $_SEP, "\n");
my $FIRE_NAMELIST_INPUT = <<'EOT';
&fire
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
EOT

# ----------------------------------------------------------------------------
#  Libraries (links and build steps taken from install_wrf_4.6.0.sh)
# ----------------------------------------------------------------------------
my @LIBS = (
    { name => "zlib", dir => "zlib-1.2.13", tar => "zlib-1.2.13.tar.gz",
      url => "https://github.com/madler/zlib/releases/download/v1.2.13/zlib-1.2.13.tar.gz",
      conf => [] },
    { name => "hdf5", dir => "hdf5-1.12.2", tar => "hdf5-1.12.2.tar.gz",
      url => "https://support.hdfgroup.org/ftp/HDF5/releases/hdf5-1.12/hdf5-1.12.2/src/hdf5-1.12.2.tar.gz",
      conf => ["--with-zlib={P}", "--enable-parallel=no", "--enable-hl", "--enable-fortran"] },
    { name => "netcdf_c", dir => "netcdf-c-4.9.2", tar => "netcdf-c-4.9.2.tar.gz",
      url => "https://github.com/Unidata/netcdf-c/archive/refs/tags/v4.9.2.tar.gz",
      conf => ["--with-hdf5={P}", "--disable-dap", "--disable-byterange"] },
    { name => "netcdf_fortran", dir => "netcdf-fortran-4.6.1", tar => "netcdf-fortran-4.6.1.tar.gz",
      url => "https://github.com/Unidata/netcdf-fortran/archive/refs/tags/v4.6.1.tar.gz",
      conf => ["--with-netcdf={P}"], env => { LIBS => "-lnetcdf -lhdf5_hl -lhdf5 -lz" } },
    { name => "mpich", dir => "mpich-4.1.2", tar => "mpich-4.1.2.tar.gz",
      url => "https://www.mpich.org/static/downloads/4.1.2/mpich-4.1.2.tar.gz", conf => [] },
    { name => "libpng", dir => "libpng-1.6.39", tar => "libpng-1.6.39.tar.gz",
      url => "https://sourceforge.net/projects/libpng/files/libpng16/1.6.39/libpng-1.6.39.tar.gz", conf => [] },
    { name => "jasper", dir => "jasper-1.900.1", tar => "jasper-1.900.1.tar.gz",
      url => "https://www2.mmm.ucar.edu/wrf/OnLineTutorial/compile_tutorial/tar_files/jasper-1.900.1.tar.gz",
      conf => [] },
    { name => "flex", dir => "flex-2.6.4", tar => "flex-2.6.4.tar.gz",
      url => "https://github.com/westes/flex/releases/download/v2.6.4/flex-2.6.4.tar.gz", conf => [] },
    { name => "byacc", dir => "byacc-*", tar => "byacc.tar.gz",
      url => "https://invisible-island.net/datafiles/release/byacc.tar.gz", conf => [] },
);

# ----------------------------------------------------------------------------
#  Terrestrial datasets and optional packages
# ----------------------------------------------------------------------------
my @GEOG_MANDATORY = ("geog_high_res_mandatory", "geog_high_res_mandatory.tar.gz");
my @CHEM_GEOGS = qw(modis_landuse_21class_30s soiltype_top_2m soiltype_bot_2m
                    albedo_ncep maxsnowalb erod clayfrac_5m sandfrac_5m);
my %ADDGEOG_GROUPS = (chem => \@CHEM_GEOGS, mandatory => ["geog_high_res_mandatory"],
                      fire => []);   # fire = local data folders only (no UCAR download)
my @UTIL_SCRIPTS = qw(setup_wrf_domain.sh run_wrf.sh download_025_gfs.sh
                      download_ecmwf_ifs_025.sh download_era5.sh download_era5.py);
my @ADDPACK_ORDER = qw(domwiz scripts cdsapi);
my %ADDPACKS = (
    domwiz  => "WRF Domain Wizard (git clone of $DOMWIZ_REPO)",
    scripts => "Your original automation shell/python scripts, saved under <home>/Scripts",
    cdsapi  => "The Python cdsapi module (pip --user) needed for ERA5 downloads",
);
my %ADDPACK_ALIASES = (dwiz => "domwiz", domainwizard => "domwiz", wizard => "domwiz",
                       utils => "scripts", script => "scripts", cds => "cdsapi", era5 => "cdsapi");

# Rough planning figures (GB) shown to the user, not guarantees.
my %SPACE_GB = (libs => 2, wrf => 3, wps => 1, geog => 30, chem_geog => 12);

my %S;   # global state, like %Install all version of wrf with options -chem, hydro, fire or arw

# =============================================================================
#  Messaging
# =============================================================================
sub say {    # no "use feature say" here, so this shadows nothing
    my ($text, $sym, $lead, $trail, $indent) = @_;
    $text   = ""  unless defined $text;
    $sym    = "*" unless defined $sym;
    $lead   = 1   unless defined $lead;
    $trail  = 1   unless defined $trail;
    $indent = 4   unless defined $indent;
    my $pad  = " " x $indent;
    my $head = $sym ne "" ? $sym . "  " : "";
    print "\n" x $lead;
    my @lines = split(/\n/, $text, -1);
    my @out;
    for my $i (0 .. $#lines) {
        push @out, ($i == 0 ? $pad . $head : $pad . (" " x length($head))) . $lines[$i];
    }
    print join("\n", @out);
    print "\n" x $trail;
    return;
}

sub info    { my $t = shift; say($t, "*", @_); }
sub warnmsg { my $t = shift; say($t, "!", @_); }
sub okmsg   { my $t = shift; say($t, "\x{2713}", @_); }

sub fatal {
    my ($text, $code) = @_;
    $text = "Something died in the pursuit of science - Again!" unless defined $text;
    say($text, "\x{2620}", 2, 1);
    say("Fix the problem above and run me again.", "\x{263A}", 0, 2);
    exit($code // 1);
}

sub on_sigint {
    say("Ouch! - Aren't we a bit sensitive?! Let's do this again sometime very soon!",
        "", 3, 2);
    exit 2;
}

sub ask {
    my ($prompt, $default) = @_;
    $default = "Yes" unless defined $default;
    return 1 if $S{allyes};
    print "    $prompt [$default]: ";
    my $ans = <STDIN>;
    $ans = "" unless defined $ans;
    chomp $ans;
    $ans =~ s/^\s+|\s+$//g;
    $ans = $default if $ans eq "";
    return ($ans =~ /^(y|ok)/i) ? 1 : 0;
}

sub ask_value {
    my ($prompt, $default) = @_;
    return $default if $S{allyes};
    print "    $prompt [$default]: ";
    my $ans = <STDIN>;
    $ans = "" unless defined $ans;
    chomp $ans;
    $ans =~ s/^\s+|\s+$//g;
    return $ans ne "" ? $ans : $default;
}

# =============================================================================
#  Process helpers
# =============================================================================
sub which {
    my ($prog) = @_;
    return "" unless defined $prog && length $prog;
    if ($prog =~ m{/}) {
        return $prog if -x $prog && !-d $prog;
        return "";
    }
    for my $d (split(/:/, ($ENV{PATH} // ""))) {
        return "$d/$prog" if -x "$d/$prog" && !-d _;
    }
    return "";
}

# _ascii_safe: emulate Python's errors="replace" when feeding possibly
# binary log tails back to the terminal.
sub _ascii_safe {
    my ($t) = @_;
    return "" unless defined $t;
    $t =~ s/[^\x09\x0a\x0d\x20-\x7e]/?/g;
    return $t;
}

sub _tmpfile {
    my $dir = eval { File::Spec->tmpdir() } || "/tmp";
    for (1 .. 100) {
        my $p = sprintf("%s/wrfall_tmp_%d_%04d", $dir, $$, int(rand(10000)));
        if (sysopen(my $fh, $p, O_CREAT() | O_EXCL() | O_RDWR(), 0600)) {
            close($fh);
            return $p;
        }
    }
    return undef;
}

sub _mkdtemp {   # minimal File::Temp::tempdir replacement (pattern with XXXXXX)
    my ($pat, $dir) = @_;
    $dir //= (eval { File::Spec->tmpdir() } || "/tmp");
    my ($pre, $suf) = $pat =~ /^(.*)XXXXXX(.*)$/s ? ($1, $2) : ($pat, "");
    for (1 .. 100) {
        my $rand = join("", map { (0 .. 9, "a" .. "z")[int(rand(36))] } 1 .. 6);
        my $p = "$dir/$pre$rand$suf";
        if (mkdir($p, 0700)) { return $p; }
    }
    return undef;
}

sub _slurp {
    my ($p) = @_;
    open(my $fh, "<:raw", $p) or return undef;
    local $/;
    my $t = <$fh>;
    close $fh;
    return defined $t ? $t : "";
}

sub _slurp_lines {
    my ($p) = @_;
    open(my $fh, "<", $p) or return ();
    my @l = <$fh>;
    close $fh;
    return @l;
}

sub _spew {
    my ($p, $t) = @_;
    open(my $fh, ">:raw", $p) or fatal("Cannot write %s: %s", $p, $!);
    print {$fh} $t;
    close $fh;
    return 1;
}

sub copy2 {   # shutil.copy2: copy + preserve mode and timestamps
    my ($src, $dst) = @_;
    copy($src, $dst) or fatal("Cannot copy %s -> %s: %s", $src, $dst, $!);
    my @st = stat($src);
    if (@st) {
        chmod($st[2] & 07777, $dst);
        utime($st[8], $st[9], $dst);
    }
    return 1;
}

sub copy_tree {   # shutil.copytree(..., symlinks=True)
    my ($src, $dst) = @_;
    make_path($dst);
    opendir(my $dh, $src) or return 0;
    while (my $ent = readdir($dh)) {
        next if $ent eq "." || $ent eq "..";
        my $s = "$src/$ent";
        my $d = "$dst/$ent";
        if (-l $s) {
            unlink($d) if (-l $d || -e $d);
            symlink(readlink($s), $d);
        }
        elsif (-d $s) { copy_tree($s, $d); }
        elsif (-f $s) { copy2($s, $d); }
    }
    closedir($dh);
    return 1;
}

sub expanduser {
    my ($p) = @_;
    return $p unless defined $p && $p =~ /^~/;
    my $home = $ENV{HOME} // "";
    return $p if $home eq "";
    return $home . substr($p, 1) if $p =~ m{^~/};
    return $home if $p eq "~";
    return $p;    # "~user" without passwd entry: left as-is
}

sub normpath {
    my ($p) = @_;
    my $abs = ($p =~ m{^/}) ? 1 : 0;
    my @out;
    for my $seg (split(m{/+}, $p)) {
        next if $seg eq "" || $seg eq ".";
        if ($seg eq "..") {
            if (@out && $out[-1] ne "..") { pop @out; }
            elsif (!$abs) { push @out, ".."; }
        }
        else { push @out, $seg; }
    }
    my $res = ($abs ? "/" : "") . join("/", @out);
    $res = "/" if $abs && $res eq "";
    $res = "." if (!$abs && $res eq "");
    return $res;
}

sub abspath {
    my ($p) = @_;
    return normpath($p) if $p =~ m{^/};
    return normpath(getcwd() . "/" . $p);
}

sub lexists_p { my ($p) = @_; return (-l $p || -e $p) ? 1 : 0; }

sub rm {   # rm -rf a file, link or directory tree
    my ($p) = @_;
    return unless defined $p;
    if (-l $p || -f $p) { unlink($p); }
    elsif (-d $p) {
        # positional form works on every File::Path (2.x hashref API is flaky on some systems)
        eval { rmtree([$p], 0, 1); 1 };
    }
    return;
}

sub cpu_count {
    my $n = 0;
    if (open(my $fh, "<", "/proc/cpuinfo")) {
        while (my $l = <$fh>) { $n++ if $l =~ /^processor\s*:/; }
        close($fh);
    }
    return $n if $n;
    my ($rc, $out) = capture_run(["getconf", "_NPROCESSORS_ONLN"], err => "devnull");
    if (defined $out && $out =~ /^\s*(\d+)/) { return int($1); }
    return 1;
}

# capture_run: run a command, capture its stdout, return (rc, output).
#   err => "devnull" | "merge" | undef (inherited)
sub capture_run {
    my ($cmd, %opt) = @_;
    my $tmp = _tmpfile();
    my $pid = fork();
    return (1, undef) unless defined $pid;
    if ($pid == 0) {
        if ($opt{env}) {
            my %e = %{ $opt{env} };
            @ENV{keys %e} = values %e;
        }
        open(STDOUT, ">", $tmp) or POSIX::_exit(127);
        if (($opt{err} // "") eq "devnull") {
            open(STDERR, ">", "/dev/null") or POSIX::_exit(127);
        }
        elsif (($opt{err} // "") eq "merge") {
            open(STDERR, ">&", \*STDOUT) or POSIX::_exit(127);
        }
        exec(@$cmd) or do { print STDERR "exec failed: $!\n"; POSIX::_exit(127); };
    }
    waitpid($pid, 0);
    my $rc = ($? & 127) ? 128 + ($? & 127) : ($? >> 8);
    my $out = -f $tmp ? _slurp($tmp) : "";
    unlink($tmp);
    return ($rc, $out);
}

# capture_t: like capture_run but the child gets alarm($timeout) (SIGALRM
# kills it), used for the flex/yacc smoke tests.
sub capture_t {
    my ($timeout, @cmd) = @_;
    my $tmp = _tmpfile();
    my $pid = fork();
    return (1, "") unless defined $pid;
    if ($pid == 0) {
        alarm($timeout) if $timeout;
        open(STDOUT, ">", $tmp) or POSIX::_exit(127);
        open(STDERR, ">&", \*STDOUT) or POSIX::_exit(127);
        exec(@cmd) or do { print STDERR "exec failed: $!\n"; POSIX::_exit(127); };
    }
    waitpid($pid, 0);
    my $timed_out = (($? & 127) == 14) ? 1 : 0;
    my $rc = ($? & 127) ? 128 + ($? & 127) : ($? >> 8);
    my $out = -f $tmp ? _slurp($tmp) : "";
    unlink($tmp);
    $out = "" unless defined $out;
    $out = "timed out after ${timeout}s" if $timed_out && $out eq "";
    $out =~ s/\s+\z//;
    return ($rc, $out);
}

sub run {
    my (%o) = @_;
    my $cmd = $o{cmd};                       # arrayref (exec list) or string (shell)
    my $shown = ref($cmd) ? join(" ", @$cmd) : $cmd;
    if ($S{debug} || $S{dryrun}) {
        say(($S{dryrun} ? "[dry-run] " : "") . $shown, "dbg", 0, 1, 6);
    }
    return 0 if $S{dryrun};

    my $log = $o{log};
    my $capf;
    if (!$log && !$o{inherit}) { $capf = _tmpfile(); $log = $capf; }   # emulate subprocess.PIPE
    my $stdin_file;
    if (defined $o{stdin}) {
        $stdin_file = _tmpfile();
        _spew($stdin_file, $o{stdin});
    }

    my $pid = fork();
    fatal("fork failed: $!") unless defined $pid;
    if ($pid == 0) {
        chdir($o{cwd}) if $o{cwd};
        if (defined $o{stdin}) {
            open(STDIN, "<", $stdin_file) or POSIX::_exit(127);
        }
        if ($log) {
            open(STDOUT, ">>", $log) or POSIX::_exit(127);
            open(STDERR, ">&", \*STDOUT) or POSIX::_exit(127);
        }
        if ($o{env}) {
            my %e = %{ $o{env} };
            @ENV{keys %e} = values %e;
        }
        if (ref($cmd)) { exec(@$cmd) or do { print STDERR "exec failed: $!\n"; POSIX::_exit(127); }; }
        else { exec("/bin/sh", "-c", $cmd) or do { print STDERR "exec failed: $!\n"; POSIX::_exit(127); }; }
    }
    waitpid($pid, 0);
    my $rc = ($? & 127) ? 128 + ($? & 127) : ($? >> 8);
    unlink($stdin_file) if defined $stdin_file;

    if (($o{check} // 1) && $rc != 0) {
        my $tail = "";
        if (-e $log) {
            my @lines = _slurp_lines($log);
            if (@lines) {
                my $from = @lines > 25 ? $#lines - 24 : 0;
                $tail = join("", @lines[$from .. $#lines]);
            }
        }
        else { $tail = "" }
        $tail = _ascii_safe($tail);
        unlink($capf) if defined $capf;
        $o{cleanup_sub}->() if ref($o{cleanup_sub}) eq "CODE";
        fatal(sprintf("Command failed (%d): %s\n\n%s", $rc, $shown, $tail));
    }
    unlink($capf) if defined $capf;
    return $rc;
}

# =============================================================================
#  Filesystem helpers used across the flows
# =============================================================================
sub _vt_num {    # "4.6.1" -> [4, 6, 1]
    my ($v) = @_;
    return [map { int $_ } ($v =~ /(\d+)/g)];
}

sub vt_cmp {     # compare two vtuple arrayrefs (like Python tuple comparison)
    my ($a, $b) = @_;
    my $n = @$a > @$b ? @$a : @$b;
    for my $i (0 .. $n - 1) {
        my $x = $a->[$i] // 0;
        my $y = $b->[$i] // 0;
        return $x <=> $y if $x != $y;
    }
    return 0;
}

sub _dedup {     # order-preserving unique
    my %seen;
    return [grep { !$seen{$_}++ } @_];
}

sub _disk_free_gb {
    my ($dir) = @_;
    my ($rc, $out) = capture_run(["df", "-Pk", $dir], err => "devnull");
    return undef if $rc != 0 || !defined $out;
    my @lines = grep { /\S/ } split(/\n/, $out);
    return undef unless @lines >= 2;
    my @c = split(/\s+/, $lines[-1]);
    return undef unless @c >= 4 && $c[3] =~ /^\d+$/;
    return $c[3] * 1024 / 1e9;
}

# =============================================================================
#  System information (CollectSystemInfo)
# =============================================================================
sub collect_system_info {
    my @u = POSIX::uname();
    my %i = (host => $u[1], kernel => $u[2], arch => $u[4]);
    $i{host} = hostname() unless defined $i{host} && length $i{host};
    my $addr = "127.0.0.1";
    eval {
        require Socket;
        my $packed = gethostbyname($i{host});
        $addr = Socket::inet_ntoa($packed) if $packed;
        1;
    };
    $i{addr} = $addr;

    my $distro = "Unknown";
    if (open(my $fh, "<", "/etc/os-release")) {
        local $/;
        my $t = <$fh>;
        close($fh);
        if (defined $t && $t =~ /PRETTY_NAME="?([^"\n]+)/) { $distro = $1; }
    }
    $i{distro} = $distro;

    my ($sockets, $cores, $threads, $model) = (1, 0, 1, "Unknown");
    my ($rc, $out) = capture_run(["lscpu"], err => "devnull", env => { LC_ALL => "C" });
    if ($rc == 0 && defined $out) {
        $out = _ascii_safe($out);
        my $g = sub {
            my ($k) = @_;
            if ($out =~ /^$k:\s*(.+)$/m) { my $v = $1; $v =~ s/\s+$//; return $v; }
            return "";
        };
        $sockets = int($g->(qr/Socket\(s\)/) || 1);
        $cores   = int($g->(qr/Core\(s\) per socket/) || 0);
        $threads = int($g->(qr/Thread\(s\) per core/) || 1);
        my $m = $g->(qr/Model name/);
        $model = $m if $m ne "";
    }
    my $total_logical = cpu_count();
    if (!$cores) {
        $cores = max(1, int($total_logical / max(1, $sockets * $threads)));
    }
    $i{sockets}         = $sockets;
    $i{cores_per_socket} = $cores;
    $i{threads_per_core} = $threads;
    $i{total_cores}     = $sockets * $cores;
    $i{model}           = $model;
    $i{ht}              = ($threads > 1 && $model !~ /amd/i) ? 1 : 0;

    my $mem = "Unknown";
    if (open(my $fh, "<", "/proc/meminfo")) {
        local $/;
        my $t = <$fh>;
        close($fh);
        if (defined $t && $t =~ /MemTotal:\s+(\d+)/) {
            $mem = sprintf("%.2f", $1 / 1048576.0);
        }
    }
    $i{mem_gb} = $mem;
    return \%i;
}

sub format_system_info {
    my ($i) = @_;
    return sprintf(
        "System information for %s:\n\n" .
        "  Machine Address     : %s\n" .
        "  Linux Kernel        : %s\n" .
        "  Linux Distribution  : %s\n\n" .
        "  CPU Name            : %s\n" .
        "  CPU Type            : %s\n" .
        "  Sockets             : %s\n" .
        "  Cores per Socket    : %s\n" .
        "  Total Physical CPUs : %s\n" .
        "  Available Memory    : %s GB",
        $i->{host}, $i->{addr}, $i->{kernel}, $i->{distro},
        $i->{model}, $i->{arch}, $i->{sockets}, $i->{cores_per_socket},
        $i->{total_cores}, $i->{mem_gb});
}

# =============================================================================
#  Release / state handling
# =============================================================================
sub release_file {
    my ($home) = @_;
    return "$home/strc/.release";
}

sub read_release {
    my ($home) = @_;
    my $f = release_file($home);
    return {} unless -f $f;
    open(my $fh, "<", $f) or return {};
    my %d;
    while (my $ln = <$fh>) {
        $ln =~ s/^\s+|\s+$//g;
        next unless length $ln;
        my ($k, $v) = $ln =~ /^(\S+)\s*(.*)$/;
        next unless defined $k;
        $d{lc $k} = defined $v ? $v : "";
    }
    close($fh);
    $d{types} = [grep { length $_ } split(/,/, ($d{types} // ""))];
    return \%d;
}

sub write_release {
    my ($home, $release, $types) = @_;
    make_path("$home/strc");
    my $old = read_release($home);
    my %merged;
    @merged{ @{ $old->{types} // [] } } = ();
    @merged{ @$types } = ();
    open(my $fh, ">", release_file($home)) or fatal("Cannot write %s: %s", release_file($home), $!);
    printf {$fh} "WRFEMS %s\nWRF %s\nWPS %s\nTYPES %s\nDATE %s\n",
        $release, $release, $release, join(",", sort keys %merged),
        POSIX::strftime("%Y-%m-%d %H:%M:%S UTC", gmtime());
    close($fh);
    return;
}

sub available_releases {
    my @rels;
    if ($HAVE_HTTP && $HAVE_JSON) {
        my $r = eval {
            HTTP::Tiny->new(timeout => 15)->get($WRF_TAGS_API,
                                                { headers => { "User-Agent" => $UA } });
        };
        if ($r && $r->{success}) {
            my $data = eval { JSON::PP->new->decode($r->{content}) };
            if (ref($data) eq "ARRAY") {
                for my $item (@$data) {
                    my $tag = $item->{tag_name} // "";
                    push @rels, $1 if $tag =~ /^v(\d+\.\d+(?:\.\d+)?)$/;
                }
            }
        }
    }
    push @rels, $DEFAULT_RELEASE unless grep { $_ eq $DEFAULT_RELEASE } @rels;
    my %seen;
    my @u = grep { !$seen{$_}++ } @rels;
    return [sort { vt_cmp(_vt_num($b), _vt_num($a)) } @u];
}

# =============================================================================
#  Download / package handling
# =============================================================================
sub http_size {
    my ($url) = @_;
    return 0 unless $HAVE_HTTP;
    my $r = eval {
        HTTP::Tiny->new(timeout => 20)->request("HEAD", $url,
                                                { headers => { "User-Agent" => $UA } });
    };
    return 0 unless $r && $r->{success};
    return int($r->{headers}{"content-length"} // 0);
}

# --- parallel segmented downloader -------------------------------------------
#  Large tarballs (WRF/WPS, WPS_GEOG, Chem datasets, ...) are fetched with
#  several concurrent HTTP range requests instead of one stream.  All hosts
#  used here (GitHub/S3, UCAR, Cloudflare-fronted mirrors) allow ranges, and
#  this typically multiplies throughput on high-latency or per-connection
#  throttled links.  Every segment keeps a <file>.pNNN marker with its
#  progress, so an interrupted transfer (Ctrl-C, drop, timeout) resumes
#  where it stopped.  --dl-jobs 1 restores plain single-stream mode.
#  (The Python port used threads; here the workers are fork()ed children and
#  a worker error is reported through a <file>.eNNN side file.)
use constant DL_CHUNK        => 1 << 20;      # read/write chunk size (1 MiB)
use constant DL_MIN_PARALLEL => 8 * (1 << 20);# parallel segments only for files >= 8 MiB
use constant DL_MAX_JOBS     => 16;           # hard cap for --dl-jobs

sub _probe {
    my ($url) = @_;
    return (0, 0) unless $HAVE_HTTP;
    my $http = HTTP::Tiny->new(timeout => 30);
    my $r = eval {
        $http->request("HEAD", $url, { headers => { "User-Agent" => $UA } });
    };
    if ($r && $r->{success}) {
        my $cl = int($r->{headers}{"content-length"} // 0);
        if ($cl) {
            my $ar = lc($r->{headers}{"accept-ranges"} // "");
            return ($cl, ($ar eq "bytes") ? 1 : 0);
        }
    }
    $r = eval {                                            # some hosts dislike HEAD: 1-byte ranged GET
        $http->get($url, { headers => { "User-Agent" => $UA, "Range" => "bytes=0-0" } });
    };
    if ($r) {
        my $st = $r->{status} // 0;
        my $cr = $r->{headers}{"content-range"} // "";
        if ($st == 206 && $cr =~ /^bytes\s+\d+-\d+\/(\d+)/) {
            return (int($1), 1);
        }
        if ($st == 200) {
            return (int($r->{headers}{"content-length"} // 0), 0);
        }
    }
    return (0, 0);
}

sub _seg_files {
    my ($part) = @_;
    return glob($part . ".p[0-9][0-9][0-9]");
}

sub _dl_cleanup {
    my ($part) = @_;
    rm($part);
    rm($_) for _seg_files($part);
    return;
}

sub _human {
    my ($n) = @_;
    return $n < (1 << 30) ? sprintf("%.1f MB", $n / 1048576.0)
                          : sprintf("%.2f GB", $n / 1073741824.0);
}

sub _fmt_rate {
    my ($bps) = @_;
    return $bps >= 1048576 ? sprintf("%5.1f MB/s", $bps / 1048576.0)
                           : sprintf("%5.0f KB/s", $bps / 1024.0);
}

sub _progress_line {
    my ($done, $total, $rate) = @_;
    my $pct = $total ? 100.0 * $done / $total : 0.0;
    my $eta = ($rate > 1 && $total > $done) ? int(($total - $done) / $rate) : 0;
    printf("\r        %5.1f%% of %-9s %s  ETA %02d:%02d:%02d   ",
           $pct, _human($total), _fmt_rate($rate),
           int($eta / 3600), int($eta % 3600 / 60), $eta % 60);
    return;
}

sub _dl_clear_line {
    return unless -t STDOUT;
    print "\r" . (" " x 76) . "\r";
    return;
}

sub _err_of {   # first error message of a worker, or ""
    my ($part, $segno) = @_;
    my $ef = sprintf("%s.e%03d", $part, $segno);
    return "" unless -f $ef;
    my $t = _slurp($ef);
    return defined $t ? $t : "";
}

# _dl_worker: child entry point; downloads bytes [start, end] of url into
# part at the correct offsets, persisting its position in <part>.pNNN.
sub _dl_worker {
    my ($url, $part, $segno, $start, $end) = @_;
    my $posf   = sprintf("%s.p%03d", $part, $segno);
    my $errf   = sprintf("%s.e%03d", $part, $segno);
    my $abortf = $part . ".abort";
    my $seg_len = $end - $start + 1;
    my $pos = 0;
    if (-f $posf) {
        my @m = split(' ', (_slurp($posf) // ""));
        if (@m >= 3 && $m[0] == $start && $m[1] == $end) {   # same segment layout only
            $pos = $m[2] + 0;
            $pos = 0 if $pos < 0;
            $pos = $seg_len if $pos > $seg_len;
        }
    }
    my $seterr = sub {
        my ($m) = @_;
        return if -f $errf;                       # first error wins (like state["err"] or ...)
        open(my $e, ">", $errf) or return;
        print {$e} $m;
        close($e);
    };
    my $abort_all = sub { open(my $a, ">", $abortf); close($a); };
    my $aborted = sub { -e $abortf ? 1 : 0 };

    open(my $pfh, ">", $posf) or do { $seterr->("cannot open $posf: $!"); return; };
    binmode($pfh);
    my $save_pos = sub {
        my ($p) = @_;
        seek($pfh, 0, 0);
        truncate($pfh, 0);
        printf {$pfh} "%d %d %d", $start, $end, $p;   # bounds stored so a changed
        $pfh->flush;                                  # --dl-jobs never mis-resumes
    };

    for my $attempt (1 .. 5) {
        last if $aborted->() || $pos >= $seg_len;
        my $stop = 0;
        my $fail;
        my $r;
        eval {
            my $http = HTTP::Tiny->new(timeout => 90);
            my $fh;
            $r = $http->get($url, {
                headers => { "User-Agent" => $UA,
                             "Range" => sprintf("bytes=%d-%d", $start + $pos, $end) },
                data_callback => sub {
                    my ($chunk, $resp) = @_;
                    die "aborted\n" if $aborted->();
                    unless ($fh) {
                        open($fh, "+<", $part) or die "cannot open $part: $!\n";
                        binmode($fh);
                        seek($fh, $start + $pos, 0);
                    }
                    print {$fh} $chunk;
                    $fh->flush;
                    $pos += length($chunk);
                    $save_pos->($pos);
                    die "aborted\n" if $aborted->();
                },
            });
            1;
        };
        if (my $e = $@) {
            my $msg = "$e";
            chomp $msg;
            if ($msg eq "aborted") { $stop = 1; }
            else { $seterr->($msg); }
        }
        elsif ($r) {
            my $st = $r->{status} // 0;
            if ($st == 416) {                    # segment already complete
                $save_pos->($seg_len);
                $stop = 1;
            }
            elsif ($st == 206) {
                if ($pos >= $seg_len) { $stop = 1; }
                else { $seterr->(sprintf("connection closed early (%d of %d segment bytes)", $pos, $seg_len)); }
            }
            elsif ($st == 401 || $st == 403 || $st == 404) {
                $seterr->("HTTP $st");
                $abort_all->();
                $stop = 1;
            }
            elsif ($st == 599) {                 # HTTP::Tiny internal error (connection level)
                my $m = $r->{content} // "connection error";
                $m = _ascii_safe($m);
                $m .= " (https needs IO::Socket::SSL, or use wget/curl)"
                    if $url =~ m{^https} && !$HAVE_SSL;
                $seterr->($m);
            }
            elsif ($st >= 400) {
                $seterr->("HTTP $st");           # retry after the backoff sleep
            }
            else {
                $seterr->("server ignored Range (HTTP $st)");
                $abort_all->();
                $stop = 1;
            }
        }
        last if $stop;
        sleep(min(2 * $attempt, 8));
    }
    $seterr->("a segment ran out of retries") if !$aborted->() && $pos < $seg_len && !-f $errf;
    close($pfh);
    return;
}

sub _seg_done {   # sum of valid per-segment positions (for the live progress line)
    my ($part, $bounds) = @_;
    my $sum = 0;
    for my $b (@$bounds) {
        my $mf = sprintf("%s.p%03d", $part, $b->[0]);
        next unless -f $mf;
        my @m = split(' ', (_slurp($mf) // ""));
        next unless @m >= 3 && $m[0] == $b->[1] && $m[1] == $b->[2];
        my $p = $m[2] + 0;
        my $len = $b->[2] - $b->[1] + 1;
        $sum += $p if $p > 0 && $p <= $len;
    }
    return $sum;
}

# _download_parallel: fetch url into part via jobs concurrent range requests
# (forked workers).  Returns (0, "") on success, (1, err) on a retryable
# failure and (2, err) when the server does not really honour range requests.
sub _download_parallel {
    my ($url, $part, $total, $jobs) = @_;
    $jobs = DL_MAX_JOBS if $jobs > DL_MAX_JOBS;
    $jobs = 2 if $jobs < 2;
    my $abortf = $part . ".abort";
    if (!scalar(_seg_files($part)) || !-f $part || (-s $part) != $total) {
        _dl_cleanup($part);
        open(my $fh, ">:raw", $part) or return (1, "cannot create $part: $!");
        truncate($fh, $total);                    # preallocate (sparse)
        close($fh);
    }
    unlink($abortf);

    my @bounds;
    my $start = 0;
    my $seg = int($total / $jobs);
    for my $i (0 .. $jobs - 1) {
        my $end = ($i < $jobs - 1) ? $start + $seg - 1 : $total - 1;
        push @bounds, [$i, $start, $end] if $end >= $start;
        $start = $end + 1;
    }

    my @kids;
    for my $b (@bounds) {
        my $pid = fork();
        if (!defined $pid) {
            open(my $e, ">", sprintf("%s.e%03d", $part, $b->[0]));
            print {$e} "fork failed: $!";
            close($e);
            next;
        }
        if ($pid == 0) {
            local $SIG{INT} = "DEFAULT";
            local $SIG{TERM} = "DEFAULT";
            _dl_worker($url, $part, $b->[0], $b->[1], $b->[2]);
            POSIX::_exit(0);
        }
        push @kids, $pid;
    }

    my $alive = scalar(@kids);
    my $t0 = time();
    my ($last, $last_t) = (0, $t0);
    while ($alive > 0) {
        sleep(0.3);
        while ((my $p = waitpid(-1, 1)) > 0) { $alive--; }   # WNOHANG
        my $now = time();
        if (-t STDOUT && $now - $last_t >= 0.4) {
            my $done = _seg_done($part, \@bounds);
            if ($done > $last) {
                _progress_line($done, $total, ($done - $last) / ($now - $last_t));
                $last = $done;
                $last_t = $now;
            }
        }
    }
    while ((my $p = waitpid(-1, 1)) > 0) { }
    1 while waitpid(-1, 1) > 0;

    my $err = "";
    for my $b (@bounds) {
        my $t = _err_of($part, $b->[0]);
        if ($t ne "" && $err eq "") { $err = $t; }
        unlink(sprintf("%s.e%03d", $part, $b->[0]));
    }
    unlink($abortf);
    return (2, $err) if $err =~ /ignored Range/;
    return (1, $err) if $err ne "";
    for my $b (@bounds) {                         # every segment must be complete
        my $mf = sprintf("%s.p%03d", $part, $b->[0]);
        my @m = split(' ', (_slurp($mf) // ""));
        unless (@m >= 3 && $m[0] == $b->[1] && $m[1] == $b->[2]
                && $m[2] == $b->[2] - $b->[1] + 1) {
            return (1, sprintf("segment %d incomplete", $b->[0]));
        }
    }
    if ((-s $part) != $total) {
        return (1, sprintf("size mismatch after download (%d != %d)", -s $part, $total));
    }
    rm($_) for _seg_files($part);
    _dl_clear_line();
    return (0, "");
}

# _download_single: plain single-stream download with resume and progress.
sub _download_single {
    my ($url, $part) = @_;
    my $err = "";
    for my $attempt (1 .. 3) {
        my $have = -e $part ? -s $part : 0;
        my %hdr = ("User-Agent" => $UA);
        $hdr{"Range"} = "bytes=$have-" if $have;
        my ($resp, $opened, $total, $done) = (undef, 0, 0, $have);
        my ($last, $last_t) = ($done, time());
        eval {
            my $http = HTTP::Tiny->new(timeout => 90);
            my $fh;
            $resp = $http->get($url, {
                headers => \%hdr,
                data_callback => sub {
                    my ($chunk, $r) = @_;
                    unless ($fh) {
                        my $st = $r->{status} // 0;
                        my $append = ($have && $st == 206) ? 1 : 0;
                        open($fh, $append ? ">>:raw" : ">:raw", $part) or die "cannot open $part: $!\n";
                        $total = int($r->{headers}{"content-length"} // 0) + ($append ? $have : 0);
                        $done  = $append ? $have : 0;
                        $last  = $done;
                        $last_t = time();
                        $opened = 1;
                    }
                    print {$fh} $chunk;
                    $done += length($chunk);
                    if (-t STDOUT && $total) {
                        my $now = time();
                        if ($now - $last_t >= 0.4) {
                            _progress_line($done, $total, ($done - $last) / ($now - $last_t));
                            $last = $done;
                            $last_t = $now;
                        }
                    }
                },
            });
            1;
        };
        if (my $e = $@) {
            my $msg = _ascii_safe("$e");
            chomp $msg;
            $err = $msg ne "" ? $msg : "unknown error";
        }
        elsif ($resp) {
            my $st = $resp->{status} // 0;
            if ($st == 416) {                    # already complete
                return (0, "");
            }
            if ($st == 200 || $st == 206) {
                unless ($opened) {               # empty body: still emulate Python's open(mode)
                    my $append = ($have && $st == 206) ? 1 : 0;
                    open(my $fh, $append ? ">>:raw" : ">:raw", $part) or do { $err = "cannot open $part: $!"; };
                    close($fh) if $fh;
                }
                _dl_clear_line();
                return (0, "");
            }
            elsif ($st == 599) {
                $err = _ascii_safe($resp->{content} // "connection error");
                $err .= " (https needs IO::Socket::SSL, or use wget/curl)"
                    if $url =~ m{^https} && !$HAVE_SSL;
            }
            else {
                $err = "HTTP $st";
            }
        }
        else {
            $err = "unknown error";
        }
        sleep(3 * $attempt);
    }
    return (1, $err);
}

# _download_urllib: download url -> part: parallel range segments when
# possible, resumable.  (Name kept from the Python original; this is the
# built-in Perl fetcher based on HTTP::Tiny.)
sub _download_urllib {
    my ($url, $part, $jobs) = @_;
    $jobs = $S{dl_jobs} // 8 unless defined $jobs;
    my $njobs = eval { int($jobs) } // 8;
    $njobs = DL_MAX_JOBS if $njobs > DL_MAX_JOBS;
    $njobs = 1 if $njobs < 1;
    my $err = "";
    return 1 unless $HAVE_HTTP;
    for my $attempt (1 .. 3) {
        my ($size, $ranges) = _probe($url);
        if ($njobs > 1 && $ranges && $size >= DL_MIN_PARALLEL) {
            my ($rc, $e) = _download_parallel($url, $part, $size, $njobs);
            $err = $e;
            return 0 if $rc == 0;
            if ($rc == 2) {                       # ranges not honoured after all
                _dl_cleanup($part);
                my ($rc2, $e2) = _download_single($url, $part);
                $err = $e2;
                return 0 if $rc2 == 0;
            }
        }
        else {
            if (scalar(_seg_files($part))) {      # leftovers from an older parallel run
                _dl_cleanup($part);
            }
            my ($rc, $e) = _download_single($url, $part);
            $err = $e;
            return 0 if $rc == 0;
        }
        sleep(3 * $attempt);
    }
    say(sprintf("Download failed: %s (%s)", $url, $err), "!", 0);
    return 1;
}

sub acquire {
    my ($url, $dest, $soft) = @_;
    my $base = basename($dest);
    if ($S{repodir}) {
        my @cands = (glob($S{repodir} . "/*/" . $base), $S{repodir} . "/" . $base);
        for my $cand (@cands) {
            next unless defined $cand;
            if (-f $cand) {
                copy2($cand, $dest) unless $S{dryrun};
                return "Copied";
            }
        }
        my $msg = sprintf("File %s not found in repository %s", $base, $S{repodir});
        return undef if $soft;
        fatal($msg);
    }
    if ($S{dryrun}) {
        say(sprintf("would download %s -> %s", $url, $dest), "dbg", 0, 1, 6);
        return "Downloaded";
    }
    my $part = $dest . ".part";
    my $m = $S{method};
    # "auto": the system downloaders were measured much faster than the built-in
    # fetcher, so try wget, then curl, and use HTTP::Tiny only as the last resort.
    my @order;
    if ($m ne "auto") {
        @order = ($m);
    }
    else {
        @order = grep { which($_) } qw(wget curl);
        push @order, "urllib" if $HAVE_HTTP;
    }
    my $rc = 1;
    for my $meth (@order) {
        if ($meth eq "wget") {
            $rc = system("wget", "-c", "-q", "--show-progress", "--progress=bar:force", "-t", "5",
                         "--connect-timeout=25", "-O", $part, $url);
            $rc = $rc >> 8 if $rc > 0 && $rc < 32768;
        }
        elsif ($meth eq "curl") {
            $rc = system("curl", "-fL", "-C", "-", "--retry", "3", "--connect-timeout", "25",
                         "-o", $part, $url);
            $rc = $rc >> 8 if $rc > 0 && $rc < 32768;
        }
        else {
            $rc = _download_urllib($url, $part);
        }
        if ($rc == 0 && -f $part && (-s $part) > 0) {
            last;
        }
        if (scalar(@order) > 1) {
            warnmsg(sprintf("%s failed for %s - trying the next method", $meth, basename($dest)), 0);
        }
    }
    if ($rc != 0 || !-f $part || (-s $part) == 0) {
        rm($part);
        my $msg = sprintf("Failed during acquisition of %s - check connection & local file system", $url);
        return undef if $soft;
        fatal($msg);
    }
    rename($part, $dest);
    return "Downloaded";
}

sub package_file {
    my ($url, $filename, $label) = @_;
    my $dest = $S{pkgdir} . "/" . $filename;
    if (-f $dest && (-s $dest) > 0 && !$S{nolocal}) {
        printf("        %-44s - Found locally\n", $filename);
        return $dest;
    }
    my $how = acquire($url, $dest);
    printf("        %-44s - %s\n", $filename, $how);
    return $dest;
}

sub unpack_tar {
    my ($tarpath, $dest_dir) = @_;
    return if $S{nounpack};
    make_path($dest_dir);
    run(cmd => ["tar", "-xf", $tarpath, "-C", $dest_dir]);
    return;
}

sub unpack_single {
    # Unpack an archive containing one top-level directory and rename it to final_dir.
    my ($tarpath, $final_dir) = @_;
    if ($S{dryrun}) {
        run(cmd => ["tar", "-xf", $tarpath, "-C", $S{home}]);
        return;
    }
    my $tmp = _mkdtemp(".unpack_XXXXXX", $S{home});
    if (!$tmp) {
        fatal("Cannot create a temporary unpack directory under %s: %s", $S{home}, $!);
    }
    run(cmd => ["tar", "-xf", $tarpath, "-C", $tmp], cleanup_sub => sub { rm($tmp); });
    opendir(my $dh, $tmp) or fatal("Cannot list %s: %s", $tmp, $!);
    my @entries = grep { $_ ne "." && $_ ne ".." } readdir($dh);
    closedir($dh);
    my $src = (scalar(@entries) == 1) ? $tmp . "/" . $entries[0] : $tmp;
    rm($final_dir);
    move($src, $final_dir) or fatal("Cannot move %s -> %s: %s", $src, $final_dir, $!);
    rm($tmp);
    return;
}

# =============================================================================
#  Paths and environment
# =============================================================================
sub set_paths {
    my ($home) = @_;
    $S{home}   = abspath(expanduser($home));
    $S{libdir} = $S{home} . "/LIBRARIES";
    $S{prefix} = $S{home} . "/local";
    $S{pkgdir} = $S{home} . "/packages";
    $S{moddir} = $S{home} . "/etc/modulefiles";
    $S{logdir} = $S{home} . "/logs";
    $S{geogdir} = $S{home} . "/WPS_GEOG";
    return;
}

sub make_dirs {
    return if $S{dryrun};
    make_path($_) for ($S{home}, $S{libdir}, $S{pkgdir}, $S{moddir}, $S{logdir},
                       $S{prefix} . "/include", $S{prefix} . "/lib", $S{prefix} . "/bin");
    return;
}

sub wrf_dir {
    my ($typ, $ver) = @_;
    return sprintf("%s/WRF-%s-%s", $S{home}, $ver, type_label($typ));
}

sub wps_dir {
    my ($typ, $ver) = @_;
    return sprintf("%s/WPS-%s-%s", $S{home}, $ver, type_label($typ));
}

sub type_label {
    my ($typ) = @_;
    return $TYPE_LABELS{$typ} // ucfirst(defined $typ ? $typ : "");
}

my $MODERN_GCC_FLAGS = "-m64 -fallow-argument-mismatch";

sub gfortran_major {
    my ($rc, $out) = capture_run(["gfortran", "-dumpversion"], err => "devnull");
    if ($rc == 0 && defined $out && $out =~ /^\s*(\d+)/) {
        return int($1);
    }
    return 99;
}

sub chem_needs_modern_gcc {
    # WRF-Chem >= 4.7 (4.7.x, 4.8.x ...) passes arguments of mixed type/rank to the same routine in
    # the chem/ sources; gfortran >= 10 turns that into a hard error unless -fallow-argument-mismatch is
    # given.  WRF 4.6.x does not need it, so its (working) build is left untouched.
    my ($typ, $ver) = @_;
    return (defined $typ && $typ eq "chem"
            && defined $ver
            && vt_cmp(_vt_num($ver), [4, 7]) >= 0
            && gfortran_major() >= 10) ? 1 : 0;
}

sub build_env {
    my ($typ, $ver) = @_;
    my $p = $S{prefix};
    my $ldlp = $ENV{LD_LIBRARY_PATH} // "";
    my %e = (
        CC => "gcc", CXX => "g++", FC => "gfortran", F77 => "gfortran",
        CFLAGS => "-O3 -fPIC", FCFLAGS => "-O3 -fPIC", FFLAGS => "-O3 -fPIC",
        LDFLAGS => "-L$p/lib", CPPFLAGS => "-I$p/include",
        DIR => $S{home}, PREFIX => $p, NETCDF => $p, HDF5 => $p,
        JASPERLIB => "$p/lib", JASPERINC => "$p/include",
        PATH => "$p/bin:" . ($ENV{PATH} // ""),
        LD_LIBRARY_PATH => ($ldlp ne "") ? "$p/lib:$ldlp" : "$p/lib",
        LC_ALL => "C",
    );
    if (defined $typ && $typ eq "chem") {
        $e{WRF_CHEM}     = "1";
        $e{WRF_KPP}      = "1";
        $e{FLEX_LIB_DIR} = "$p/lib";
        $e{FLEX}         = "$p/bin/flex";
        $e{YACC}         = "$p/bin/yacc -d";
        if (chem_needs_modern_gcc($typ, $ver)) {
            $e{FCFLAGS} = "-O3 -fPIC $MODERN_GCC_FLAGS";
            $e{FFLAGS}  = "-O3 -fPIC $MODERN_GCC_FLAGS";
        }
    }
    elsif (defined $typ && $typ eq "hydro") {
        $e{WRF_HYDRO} = "1";
    }
    return \%e;
}

sub clean_environ_for_build {
    delete @ENV{qw(WRF_CHEM WRF_KPP WRF_HYDRO FLEX_LIB_DIR FLEX YACC)};
    return;
}

# =============================================================================
#  Prerequisites
# =============================================================================
sub check_prereqs {
    my @need = qw(gcc g++ gfortran make tar perl);
    my @miss = grep { !which($_) } @need;
    if (@miss && $S{dryrun}) {
        warnmsg("Missing build tools (ignored in --dryrun): " . join(", ", @miss));
    }
    elsif (@miss) {
        fatal(sprintf("Missing required build tools: %s\n\n" .
              "Install them with your package manager, for example:\n" .
              "    Debian/Ubuntu : sudo apt install build-essential gfortran perl tcsh m4 git\n" .
              "    RHEL/Rocky    : sudo dnf groupinstall 'Development Tools' && sudo dnf install gcc-gfortran perl tcsh m4 git",
              join(", ", @miss)));
    }
    my @soft;
    if (!which("tcsh") && !which("csh")) {
        push @soft, "tcsh/csh (WPS compile and link_grib.csh need it)";
    }
    if (!which("m4")) {
        push @soft, "m4 (needed by some library builds)";
    }
    if (($S{method} eq "curl" || $S{method} eq "wget") && !which($S{method})) {
        fatal(sprintf("You requested --%s but it is not installed.", $S{method}));
    }
    if (@soft) {
        warnmsg("Optional tools not found: " . join("; ", @soft));
    }
    return;
}

sub disk_estimate_gb {
    my ($types, $nogeog, $chem) = @_;
    my $t = $SPACE_GB{libs} + scalar(@$types) * ($SPACE_GB{wrf} + $SPACE_GB{wps});
    $t += $SPACE_GB{geog} unless $nogeog;
    $t += $SPACE_GB{chem_geog} if $chem && !$nogeog;
    return $t;
}

# =============================================================================
#  Library builds
# =============================================================================
sub marker {
    my ($name) = @_;
    return sprintf("%s/.installed_%s", $S{prefix}, $name);
}

# files that prove a support library is really installed under <prefix>
my %LIB_PROBES = (
    zlib            => ["lib/libz.*", "include/zlib.h"],
    hdf5            => ["lib/libhdf5.*", "lib/libhdf5_hl.*", "include/hdf5.h"],
    netcdf_c        => ["lib/libnetcdf.*", "bin/nc-config"],
    netcdf_fortran  => ["lib/libnetcdff.*", "bin/nf-config"],
    mpich           => ["bin/mpif90", "bin/mpiexec", "lib/libmpi*"],
    libpng          => ["lib/libpng*", "include/png.h"],
    jasper          => ["lib/libjasper.*", "include/jasper/jasper.h"],
    flex            => ["bin/flex", "lib/libfl.*"],
    byacc           => ["bin/yacc"],
);

sub lib_present {
    # True when every probe file of the library exists under <prefix> (marker file not needed).
    my ($name) = @_;
    my $pats = $LIB_PROBES{$name};
    if (!$pats) {
        return -f marker($name) ? 1 : 0;
    }
    for my $pat (@$pats) {
        my @m = glob($S{prefix} . "/" . $pat);
        return 0 unless @m;
    }
    return 1;
}

sub lib_done {
    # Skip a library that is already installed.  --force rebuilds WRF/WPS only; use
    # --force-libs to rebuild the support libraries as well.
    my ($name) = @_;
    return 0 if $S{force_libs} || $S{dryrun};
    return lib_present($name);
}

sub build_libraries {
    info(sprintf("Building %d support libraries into %s", scalar(@LIBS), $S{prefix}), 2);
    my $n = scalar(@LIBS);
    my $idx = 0;
    for my $lib (@LIBS) {
        $idx++;
        my $name = $lib->{name};
        if (lib_done($name)) {
            say(sprintf("[%d/%d] %s already installed (skipping)", $idx, $n, $name), "\x{2713}", 0, 1, 6);
            if (!-f marker($name) && !$S{dryrun}) {
                _spew(marker($name), "");
            }
            next;
        }
        say(sprintf("[%d/%d] %s", $idx, $n, $name), "->", 0, 1, 6);
        my $tarp = package_file($lib->{url}, $lib->{tar});
        my $env = build_env();
        my %env2 = (%$env, %{ $lib->{env} // {} });
        my $log = sprintf("%s/lib_%s.log", $S{logdir}, $name);
        rm($log) unless $S{dryrun};
        # unpack
        my $pattern = $S{libdir} . "/" . $lib->{dir};
        my @have = glob($pattern);
        if (!@have || $S{force} || !$S{continue}) {
            rm($_) for @have;
            unpack_tar($tarp, $S{libdir});
            @have = glob($pattern);
        }
        fatal(sprintf("Unpacking %s did not create %s", $lib->{tar}, $lib->{dir}))
            if !@have && !$S{dryrun};
        my $src = @have ? $have[0] : $pattern;
        my @conf = map { my $c = $_; $c =~ s/\{P\}/$S{prefix}/g; $c } @{ $lib->{conf} };
        run(cmd => ["./configure", "--prefix=" . $S{prefix}, @conf], cwd => $src, env => \%env2, log => $log);
        run(cmd => ["make", "-j" . $S{makej}], cwd => $src, env => \%env2, log => $log);
        run(cmd => ["make", "install"], cwd => $src, env => \%env2, log => $log);
        _spew(marker($name), "") unless $S{dryrun};
    }
    okmsg(sprintf("All libraries installed to %s (%s)", $S{prefix},
                  $S{force_libs} ? "rebuilt" : "existing ones reused"), 1);
    return;
}

# =============================================================================
#  WRF / WPS builds
# =============================================================================
sub check_kpp_tools {
    # WRF-Chem (-chem) builds KPP, which needs yacc + flex + libfl from <prefix>. Stop early if absent.
    return if $S{dryrun};
    my $p = $S{prefix};
    my @miss = grep { !-f "$p/bin/$_" } qw(yacc flex);
    my @libs = glob("$p/lib*/libfl.*");
    push @miss, "libfl (flex library)" unless @libs;
    if (@miss) {
        fatal(sprintf("WRF-Chem needs yacc, flex and libfl for KPP, but %s not found under %s.\n" .
              "Rebuild them from source and retry:\n" .
              "    rm -f %s/.installed_flex %s/.installed_byacc\n" .
              "    %s --install -chem --continue --force\n" .
              "KPP needs flex built from SOURCE: the Debian/Ubuntu apt flex package is broken and\n" .
              "makes every mechanism fail ('third body ... M', Error 127). Distro packages are only\n" .
              "a fallback (sudo apt install byacc flex libfl-dev / dnf install byacc flex flex-devel).",
              join(", ", @miss), $p, $p, $p, $EXE));
    }
    # Functional smoke test: a broken flex (notably the Debian/Ubuntu apt package) or a parser
    # generated by bison 3.x makes every KPP mechanism fail later with Error 127 /
    # 'third body ... M' / a segfault.  Catch that here instead of after 40 minutes of compiling.
    my (undef, $out) = capture_t(30, "$p/bin/flex", "--version");
    $out = _ascii_safe($out // "");
    if ($out !~ /flex/i) {
        fatal(sprintf("The flex binary at %s/bin/flex is broken (\"%s\").\n" .
              "A broken flex breaks every WRF-Chem KPP mechanism ('third body ... M', Error 127).\n" .
              "Rebuild flex 2.6.4 from source and retry:\n" .
              "    rm -f %s/.installed_flex\n" .
              "    %s --install -chem --continue --force", $p, $out, $p, $EXE));
    }
    say("flex OK: $out", "\x{2713}", 0, 1, 6);
    my (undef, $out2) = capture_t(30, "$p/bin/yacc", "-V");
    $out2 = _ascii_safe($out2 // "");
    if (defined $out2 && $out2 ne "") {
        say("yacc OK: $out2", "\x{2713}", 0, 1, 6);
    }
    else {
        warnmsg(sprintf("could not run %s/bin/yacc -V (continuing anyway)", $p), 0, 1, 6);
    }
    return;
}

my $ERR_RE = qr/(?i)(\berror\b[: ]|\*\*\* \[|undefined reference|multiple definition|
segmentation fault|command not found|no such file|cannot find|fatal|
unexpected case|third body|core dumped)/;

sub error_excerpt {
    # First n real error lines of a build log, with line numbers (the first error is the cause;
    # everything after it is usually fallout).
    my ($log, $n) = @_;
    $n //= 14;
    return unless -f $log;
    my @lines = _slurp_lines($log);
    my @hits;
    my $i = 0;
    for my $l (@lines) {
        $i++;
        my $strip = $l;
        $strip =~ s/^\s+|\s+$//g;
        next unless $strip =~ $ERR_RE;
        next if $l =~ /Error: 0/;
        next if $l =~ /^\s*#/;
        push @hits, [$i, $strip];
    }
    return unless @hits;
    my @body_parts;
    for my $h (@hits[0 .. min($n, scalar(@hits)) - 1]) {
        push @body_parts, sprintf("   %6d: %s", $h->[0], substr($h->[1], 0, 170));
    }
    my $body = join("\n", @body_parts);
    warnmsg(sprintf("First errors in %s (the FIRST one is the cause, the rest is fallout):\n%s\n" .
                    "   Context:  sed -n '%d,%dp' %s",
                    $log, $body, max(1, $hits[0][0] - 12), $hits[0][0] + 8, $log), 1);
    return;
}

sub kpp_hint {
    # After a failed chem compile: explain the usual KPP failure signatures.
    my ($log, $work) = @_;
    error_excerpt($log);
    return unless -f $log;
    my $txt = _slurp($log);
    my $wkc_sig = ($txt =~ /undefined reference to `\S*jacobiansp\S*_lu_(diag|crow|icol)/
                   || index($txt, "Unexpected CASE statement") >= 0
                   || index($txt, "case-selector exists") >= 0) ? 1 : 0;
    if ($wkc_sig) {
        warnmsg(sprintf("This is the WRF-Chem 4.7.x 'tuv_kpp FIRST' path bug in chem/KPP/compile_wkc, not a flex/yacc problem:\n" .
              "the photolysis include files are never generated, which explains the KPP segfault, the\n" .
              "'undefined reference ... jacobiansp ... lu_diag', Error 127 and module_phot_tuv's\n" .
              "'Unexpected CASE statement'.  The installer now patches compile_wkc automatically\n" .
              "(path -> ../../../../inc/); rebuild with:\n" .
              "    %s --install %s -chem --force\n" .
              "By hand: edit chem/KPP/compile_wkc so the line reads\n" .
              "    \$WKC_HOME/util/wkc/tuv_kpp FIRST ../../../../inc/\n" .
              "and rebuild from a freshly unpacked tree (the installer does this with --force).",
              $EXE, $S{ver_hint} // "<version>"), 1);
        return;
    }
    my @kpp_bins = $work ? glob("$work/chem/KPP/kpp/kpp-*/bin/kpp") : ();
    if ($work && !@kpp_bins) {
        warnmsg(sprintf("The KPP program (chem/KPP/kpp/kpp-2.1/bin/kpp) was not built - the kpp build itself failed.\n" .
              "Fix the flex/yacc/libfl setup and rebuild:\n" .
              "    rm -f %s/.installed_flex %s/.installed_byacc\n" .
              "    %s --install -chem --force-libs --force", $S{prefix}, $S{prefix}, $EXE), 1);
    }
    if (index($txt, "third body in KPP") >= 0 || index($txt, "Error 127") >= 0) {
        warnmsg(sprintf("KPP failed (Error 127 / 'third body ... M'). Rebuild with the local tools - in the SAME shell:\n" .
              "  conda deactivate 2>/dev/null           # conda's flex/bison shadow the local ones\n" .
              "  export WRF_CHEM=1 WRF_KPP=1 PATH=%s/bin:\$PATH\n" .
              "  export YACC=\"%s/bin/yacc -d\" FLEX=\"%s/bin/flex\" FLEX_LIB_DIR=%s/lib\n" .
              "KPP needs byacc (not bison 3.x) and flex built from source - the Debian/Ubuntu apt flex\n" .
              "breaks every mechanism with exactly this error. WRF >= 4.7 also has a known KPP build bug\n" .
              "(wrf-model/wrf issue #2259); if it still fails, use WRF 4.6.x:\n" .
              "  %s --install 4.6.0 -chem --force",
              $S{prefix}, $S{prefix}, $S{prefix}, $S{prefix}, $EXE), 1);
    }
    if ($txt =~ /(Type|Rank) mismatch|fallow-argument-mismatch|Modern GCC compat/) {
        warnmsg(sprintf("gfortran rejected mixed-type/rank arguments in the WRF-Chem sources (GCC >= 10).\n" .
              "This installer adds the flags for WRF-Chem >= 4.7 automatically; if you compile by hand, use:\n" .
              "    export FCFLAGS=\"-m64 -fallow-argument-mismatch\"\n" .
              "    export FFLAGS=\"-m64 -fallow-argument-mismatch\"\n" .
              "    (then re-run ./configure and add the flag to FCBASEOPTS_NO_G in configure.wrf)\n" .
              "or rebuild:  %s --install %s -chem --force", $EXE, "<version>"), 1);
    }
    if (index($txt, "Segmentation fault") >= 0) {
        warnmsg(sprintf("A KPP-stage command segfaulted - the typical broken-flex (apt) signature. Rebuild it:\n" .
              "    %s --install -chem --force-libs --force", $EXE), 1);
    }
    if (index($txt, "Unexpected CASE statement") >= 0) {
        warnmsg(sprintf("module_phot_tuv failed with 'Unexpected CASE statement': check\n" .
              "  grep -n -B3 -A3 'Unexpected CASE' %s", $log), 1);
    }
    return;
}

sub force_kpp_tool_paths {
    # ./configure records the flex/yacc it found in configure.wrf, and those Makefile
    # assignments override the exported FLEX/YACC environment variables.  Re-point them at
    # the local byacc/flex so the whole KPP stage uses the right tools (system bison or the
    # Debian/Ubuntu apt flex produce 'third body ... M', per-mechanism Error 127, segfaults).
    my ($work) = @_;
    return if $S{dryrun};
    my $p = $S{prefix};
    my $cw = "$work/configure.wrf";
    return unless -f $cw;
    my $txt = _slurp($cw);
    my $n = ($txt =~ s{^(\s*YACC\s*=\s*).*$}{$1 . $p . "/bin/yacc -d"}gme);
    my $k = ($txt =~ s{^(\s*FLEX\s*=\s*).*$}{$1 . $p . "/bin/flex"}gme);
    $n += $k;
    if ($n) {
        _spew($cw, $txt);
        say(sprintf("configure.wrf: %d YACC/FLEX assignment(s) re-pointed to %s/bin", $n, $p),
            "\x{2713}", 0, 1, 6);
    }
    else {
        warnmsg(sprintf("no YACC/FLEX assignments found in %s - KPP tools will be taken from PATH\n" .
              "(%s/bin is first there, so byacc/flex from this installer win)", $cw, $p), 0, 1, 6);
    }
    return;
}

sub fix_compile_wkc {
    # WRF-Chem 4.7.0/4.7.1 (old make-based build) bug in chem/KPP/compile_wkc: the first
    # 'tuv_kpp FIRST <dir>' call gets a too-short relative path to WRF's inc/ directory, so the
    # photolysis include files are never written.  Consequences, all seen in the log:
    #   * 'Segmentation fault' from the KPP/WKC Makefile step,
    #   * undefined reference to '<mech>_jacobiansp_MOD_lu_diag/lu_crow/lu_icol' (write_decomp),
    #   * 'Error 127' (write_decomp.exe was never produced),
    #   * module_phot_tuv.f90: 'Unexpected CASE statement'.
    # The WRF developers' fix (UCAR forum, threads 22663 and 22757): the path must be ../../../../inc/.
    # Idempotent: a tree that already has the right path (e.g. a later release) is left alone.
    my ($work) = @_;
    return if $S{dryrun};
    my $f = "$work/chem/KPP/compile_wkc";
    if (!-f $f) {
        warnmsg(sprintf("%s not found - cannot apply the tuv_kpp path fix", $f), 0, 1, 6);
        return;
    }
    my $txt = _slurp($f);
    my $want = "../../../../inc/";
    my $pat = qr/(tuv_kpp\s+FIRST\s+)((?:\.\.\/)+inc\/)/;
    if ($txt =~ $pat) {
        my ($g1, $g2) = ($1, $2);
        if ($g2 eq $want) {
            say("compile_wkc: tuv_kpp FIRST path already correct", "\x{2713}", 0, 1, 6);
        }
        else {
            $txt =~ s/$pat/$1 . $want/ge;
            _spew($f, $txt);
            say(sprintf("compile_wkc: 'tuv_kpp FIRST %s' -> '%s' (WRF-Chem 4.7.x bug fix)", $g2, $want),
                "\x{2713}", 0, 1, 6);
        }
    }
    else {
        warnmsg(sprintf("no 'tuv_kpp FIRST ...inc/' line found in %s - nothing to fix (check it by hand if KPP fails)", $f),
                0, 1, 6);
    }
    return;
}

sub force_modern_gcc_flags {
    # ./configure writes its own compiler flags into configure.wrf and those override the exported
    # FCFLAGS/FFLAGS, so add -fallow-argument-mismatch (and -m64) there too.  Idempotent.
    my ($work) = @_;
    return if $S{dryrun};
    my $cw = "$work/configure.wrf";
    return unless -f $cw;
    my $txt = _slurp($cw);
    my $n = 0;
    $txt =~ s{^(FCBASEOPTS_NO_G|F77OPTS)\s*=.*$}{
        my $line = $&;
        my @missing = grep { index($line, $_) < 0 } split(/\s+/, $MODERN_GCC_FLAGS);
        if (!@missing) { $line }
        else {
            $n++;
            (my $lr = $line) =~ s/\s+$//;
            $lr . " " . join(" ", @missing);
        }
    }gme;
    if ($n) {
        _spew($cw, $txt);
        say(sprintf("configure.wrf: %s added to %d Fortran flag line(s)", $MODERN_GCC_FLAGS, $n),
            "\x{2713}", 0, 1, 6);
    }
    elsif ($txt !~ /^(FCBASEOPTS_NO_G|F77OPTS)\s*=.*-fallow-argument-mismatch/m) {
        warnmsg(sprintf("no FCBASEOPTS_NO_G line found in %s; the flags (%s) are taken from the environment only",
                        $cw, $MODERN_GCC_FLAGS), 0, 1, 6);
    }
    return;
}

sub build_wrf {
    my ($typ, $ver) = @_;
    my $wd = wrf_dir($typ, $ver);
    my $log = sprintf("%s/wrf_%s_%s.log", $S{logdir}, $typ, $ver);
    if (-f "$wd/main/wrf.exe" && !$S{force}) {
        info(sprintf("WRF %s (%s) already built (use --force to rebuild)", $ver, type_label($typ)), 1);
        return;
    }
    info(sprintf("Downloading and compiling WRF v%s (%s) - log: %s", $ver, type_label($typ), $log), 2);
    if ($typ eq "chem") {
        check_kpp_tools();
        if (vt_cmp(_vt_num($ver), [4, 7]) >= 0) {
            warnmsg(sprintf("WRF >= 4.7 has a known KPP build problem with recent GCC (wrf-model/wrf issue #2259:\n" .
                  "'third body ... M' + one 'Error 127' per chemistry mechanism). This installer forces\n" .
                  "the local byacc/flex into the build, which fixes most cases; if the compile still\n" .
                  "fails, install WRF 4.6.x instead: %s --install 4.6.0 -chem --force", $EXE), 1);
        }
    }
    my $env = build_env($typ, $ver);
    my $modern = chem_needs_modern_gcc($typ, $ver);
    if ($modern) {
        info(sprintf("WRF-Chem %s with gfortran %d: adding %s (modern GCC compatibility)",
                     $ver, gfortran_major(), $MODERN_GCC_FLAGS), 0);
    }
    my $tarp = package_file(sprintf($WRF_URL, $ver, $ver), "WRFV$ver.tar.gz");
    # unpack to a temporary name, build there (WRF dislikes being moved after configure)
    my $work = sprintf("%s/WRFV%s", $S{home}, $ver);
    unpack_single($tarp, $work);
    if ($typ eq "hydro") {
        my $htar = package_file($HYDRO_URL, "wrf_hydro_v5.3.0.tar.gz");
        my $tmp = sprintf("%s/_hydro_tmp", $S{home});
        unpack_single($htar, $tmp);
        if (!$S{dryrun}) {
            my $src = "$tmp/trunk/NDHMS";
            run(cmd => "cp -rf $src/* $work/hydro/", shell => 1);
            rm($tmp);
        }
    }
    rm($log) unless $S{dryrun};
    run(cmd => ["./configure"], cwd => $work, env => $env, log => $log,
        stdin => "$S{wrf_opt}\n$S{nest_opt}\n");
    if ($typ eq "chem") {
        force_kpp_tool_paths($work);      # configure.wrf YACC/FLEX -> local byacc/flex
        if ($modern) {
            force_modern_gcc_flags($work);   # configure.wrf compile flags -> -fallow-argument-mismatch
        }
        if (vt_cmp(_vt_num($ver), [4, 7]) >= 0) {
            fix_compile_wkc($work);          # WRF-Chem 4.7.x tuv_kpp output-path bug
        }
    }
    # old gfortran quirk handled in the original script
    my $gv = gfortran_major();
    my $cw = "$work/configure.wrf";
    if (!$S{dryrun} && $gv >= 6 && $gv < 8 && -f $cw) {
        my @lines = grep { index($_, "-DBUILD_RRTMG_FAST=1") < 0 } _slurp_lines($cw);
        open(my $fh, ">", $cw);
        print {$fh} @lines;
        close($fh);
    }
    my $target = ($typ eq "fireideal") ? "em_fire" : "em_real";
    my @needed = ("wrf.exe");
    push @needed, "ideal.exe" if $typ eq "fireideal";
    info(sprintf("Compiling WRF %s (%s, ./compile %s) - this can take 20-40+ minutes",
                 $ver, type_label($typ), $target), 0);
    run(cmd => ["./compile", $target], cwd => $work, env => $env, log => $log, check => 0);
    if (!$S{dryrun}) {
        my $bad = (-f $log && index(_slurp($log), "Problems building executables") >= 0) ? 1 : 0;
        my $missing = 0;
        for my $x (@needed) { $missing = 1 unless -f "$work/main/$x"; }
        if ($bad || $missing) {
            if ($typ eq "chem") {
                $S{ver_hint} = $ver;
                kpp_hint($log, $work);
            }
            fatal(sprintf("WRF %s compilation failed. See %s", type_label($typ), $log));
        }
        rm($wd);
        move($work, $wd) or fatal("Cannot move %s -> %s: %s", $work, $wd, $!);
    }
    okmsg(sprintf("WRF %s built: %s", type_label($typ), $wd));
    return;
}

sub build_wps {
    my ($typ, $ver) = @_;
    my $pd = wps_dir($typ, $ver);
    my $wd = wrf_dir($typ, $ver);
    my $log = sprintf("%s/wps_%s_%s.log", $S{logdir}, $typ, $ver);
    if (-f "$pd/geogrid.exe" && !$S{force}) {
        info(sprintf("WPS %s (%s) already built (use --force to rebuild)", $ver, type_label($typ)), 1);
        return;
    }
    info(sprintf("Downloading and compiling WPS v%s (%s) - log: %s", $ver, type_label($typ), $log), 2);
    my $env = build_env($typ);
    $env->{WRF_DIR} = $wd;
    my $tarp = package_file(sprintf($WPS_URL, $ver), "WPSV$ver.tar.gz");
    unpack_single($tarp, $pd);
    rm($log) unless $S{dryrun};
    run(cmd => ["./clean"], cwd => $pd, env => $env, log => $log, check => 0);
    run(cmd => ["./configure"], cwd => $pd, env => $env, log => $log, stdin => "$S{wps_opt}\n");
    run(cmd => ["./compile"], cwd => $pd, env => $env, log => $log, check => 0);
    if (!$S{dryrun}) {
        for my $exe (qw(geogrid.exe ungrib.exe metgrid.exe)) {
            fatal(sprintf("WPS %s compilation failed (%s missing). See %s", type_label($typ), $exe, $log))
                unless -f "$pd/$exe";
        }
        my $nl = "$pd/namelist.wps";
        set_namelist_text($nl, "geog_data_path", "'$S{geogdir}/'", 1);
        setup_fire_wps($pd) if $typ eq "fire";
    }
    okmsg(sprintf("WPS %s built: %s", type_label($typ), $pd));
    return;
}

# =============================================================================
#  WRF-Fire helpers
# =============================================================================
sub setup_fire_wps {
    # GEOGRID.TBL gets NFUEL_CAT and ZSF (subgrid=yes); namelist.wps gets subgrid_ratio_x/y.
    my ($pd) = @_;
    my $gdir = "$pd/geogrid";
    my $base = "$gdir/GEOGRID.TBL.ARW";
    my $fire = "$gdir/GEOGRID.TBL.ARW_FIRE";
    if (!-f $base) {
        warnmsg("$base not found - add NFUEL_CAT/ZSF to GEOGRID.TBL by hand (see the WRF-Fire chapter).");
    }
    else {
        my $txt = _slurp($base);
        $txt =~ s/\n+\z//;
        if (index($txt, "NFUEL_CAT") < 0) {
            my $last = "";
            for my $l (split(/\n/, $txt)) { $last = $l if $l =~ /\S/; }
            $txt .= "\n" . (($last =~ /^=/) ? "" : $_SEP . "\n") . $FIRE_GEOGRID_ENTRIES;
        }
        else {
            $txt .= "\n";
        }
        _spew($fire, $txt);
        my $link = "$gdir/GEOGRID.TBL";
        rm($link);
        symlink("GEOGRID.TBL.ARW_FIRE", $link);
        say("geogrid/GEOGRID.TBL -> GEOGRID.TBL.ARW_FIRE (NFUEL_CAT, ZSF on the fire subgrid)",
            "\x{2713}", 0, 1, 6);
    }
    add_wps_subgrid("$pd/namelist.wps");
    return;
}

sub add_wps_subgrid {
    # Insert subgrid_ratio_x/y into &share of namelist.wps if missing.
    my ($nl) = @_;
    return if $S{dryrun} || !-f $nl;
    my $txt = _slurp($nl);
    return if $txt =~ /^\s*subgrid_ratio_x/im;
    my $n = ($txt =~ s/^(\s*max_dom\s*=.*)$/my $g1 = $1;
             "$g1\n subgrid_ratio_x = $FIRE_SR,\n subgrid_ratio_y = $FIRE_SR,"/ime);
    if ($n) {
        _spew($nl, $txt);
    }
    else {
        warnmsg(sprintf("Could not find max_dom in %s - add subgrid_ratio_x/y = %d to &share yourself.",
                        $nl, $FIRE_SR));
    }
    return;
}

sub fire_prepare_domain {
    # Make a run domain WRF-Fire ready: sr_x/sr_y, &fire block, namelist.fire, subgrid_ratio.
    my ($dom, $rdir) = @_;
    add_wps_subgrid("$dom/namelist.wps");
    my $nl = "$dom/namelist.input";
    if (-f $nl) {
        my $txt = _slurp($nl);
        if ($txt !~ /^\s*sr_x\s*=/im) {
            $txt =~ s/^(\s*&domains\s*)$/my $g1 = $1;
                     "$g1\n sr_x = $FIRE_SR,\n sr_y = $FIRE_SR,"/ime;
        }
        if ($txt !~ /^\s*&fire/im) {
            $txt =~ s/\n+\z//;
            $txt .= "\n\n" . $FIRE_NAMELIST_INPUT;
        }
        _spew($nl, $txt);
    }
    my $nf = "$dom/namelist.fire";
    my $src = "$rdir/namelist.fire";
    unlink($nf) if -l $nf;
    if (!-f $nf && -f $src) {
        copy2($src, $nf);          # editable copy for custom fuel categories
    }
    say(sprintf("WRF-Fire settings added to the domain (sr_x/sr_y=%d, &fire, subgrid_ratio_x/y=%d).",
                $FIRE_SR, $FIRE_SR), "\x{2713}", 0, 1, 6);
    return;
}

sub setup_fire_geog {
    # Folders for the two extra WRF-Fire static datasets (not available from UCAR).
    info(sprintf("Preparing WRF-Fire static data folders in %s", $S{geogdir}), 2);
    my $txt = sprintf("WRF-Fire needs two datasets that are NOT part of the standard WPS_GEOG download:\n\n" .
           "  NFUEL_CAT  Anderson 13 fuel categories (e.g. USGS LANDFIRE 'Anderson Fire Behavior Fuel Models')\n" .
           "             -> convert to geogrid binary format and put it in  %s/landfire/\n" .
           "  ZSF        high-resolution terrain height (1/3 or 1 arc-second DEM, e.g. USGS National Map)\n" .
           "             -> convert to geogrid binary format and put it in  %s/highres_elev/\n\n" .
           "Each folder needs an 'index' file (see the WPS chapter of the Users Guide for the geogrid format).\n" .
           "GEOGRID.TBL of the Fire WPS build already points at these folders (rel_path, subgrid=yes).\n" .
           "NOTE: halt_on_missing=yes - geogrid.exe stops if either dataset is missing for your domain.\n",
           $S{geogdir}, $S{geogdir});
    if (!$S{dryrun}) {
        for my $d (@FIRE_GEOG_DIRS) {
            make_path($S{geogdir} . "/" . $d);
        }
        _spew($S{geogdir} . "/README_WRF_FIRE.txt", $txt);
    }
    for my $d (@FIRE_GEOG_DIRS) {
        say(sprintf("%s/%s/", $S{geogdir}, $d), "\x{2713}", 0, 1, 6);
    }
    print join("\n", map { "      " . $_ } split(/\n/, $txt, -1)), "\n";
    return;
}

sub set_namelist_text {
    # Replace 'key = ...' in a namelist while preserving trailing comments.
    my ($path, $key, $value, $single) = @_;
    return 0 unless -f $path;
    open(my $fh, "<", $path) or return 0;
    my @lines = <$fh>;
    close($fh);
    my $pat = qr/^(\s*\Q$key\E\s*=\s*)(.*?)(\s*(?:!.*)?)(\r?\n?)$/i;
    for my $i (0 .. $#lines) {
        next unless $lines[$i] =~ $pat;
        my ($g1, $g2, $g3, $g4) = ($1, $2, $3, $4);
        my $old = $g2;
        $old =~ s/^\s+|\s+$//g;
        my $vals;
        if ($single) {
            $vals = $value;
        }
        else {
            my $commas = () = ($old =~ /,/g);
            my $count = $commas + (($old =~ /,\z/) ? 0 : 1);
            $count = 1 if $count < 1;
            $vals = join(", ", ($value) x $count) . (($old =~ /,\z/) ? "," : "");
        }
        $lines[$i] = $g1 . $vals . $g3 . $g4;
        open(my $out, ">", $path) or fatal("Cannot write %s: %s", $path, $!);
        print {$out} @lines;
        close($out);
        return 1;
    }
    return 0;
}

# =============================================================================
#  Geography data
# =============================================================================
sub install_geog {
    my ($extra, $chem) = @_;
    my @names = @{ $extra // [] };
    if ($chem) {
        for my $n (@CHEM_GEOGS) {
            push @names, $n unless grep { $_ eq $n } @names;
        }
    }
    if ($S{nogeog} && !@names) {
        info("Skipping WPS_GEOG (--nogeog)");
        return;
    }
    info(sprintf("Setting up terrestrial datasets in %s", $S{geogdir}), 2);
    if (!$S{nogeog}) {
        if (-d $S{geogdir} && !$S{force} && !grep { $_ eq "geog_high_res_mandatory" } @names) {
            say("Base datasets already present", "\x{2713}", 0, 1, 6);
        }
        else {
            my $tarp = package_file($GEOG_BASE . "/" . $GEOG_MANDATORY[1], $GEOG_MANDATORY[1]);
            unpack_tar($tarp, $S{home});
        }
    }
    for my $n (@names) {
        next if $n eq "geog_high_res_mandatory";
        my $d = $S{geogdir} . "/" . $n;
        if (-d $d && !$S{force}) {
            say("$n already present", "\x{2713}", 0, 1, 6);
            next;
        }
        my $fn = "$n.tar.bz2";
        my $tarp = package_file($GEOG_BASE . "/" . $fn, $fn);
        make_path($S{geogdir}) unless $S{dryrun};
        unpack_tar($tarp, $S{geogdir});
    }
    return;
}

# =============================================================================
#  Environment files, Do-List (SystemConfig / PrintGuidance in Build_WRF)
# =============================================================================
my $TCL_T = <<'EOT';
#%Module1.0
## WRF @TYPE@ @VER@ environment - generated by wrfall_install.pl
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
EOT

my $SH_T = <<'EOT';
# WRF @TYPE@ @VER@ environment (bash) - source this file
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
EOT

my $CSH_T = <<'EOT';
# WRF @TYPE@ @VER@ environment (tcsh) - source this file
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
EOT

sub write_environment_files {
    my ($typ, $ver) = @_;
    my $lab = type_label($typ);
    my $extras;
    if ($typ eq "chem") {
        $extras = {
            tcl => "setenv WRF_CHEM 1\nsetenv WRF_KPP 1\nsetenv FLEX_LIB_DIR \$prefix/lib\n" .
                   "setenv FLEX \$prefix/bin/flex\nsetenv YACC \"\$prefix/bin/yacc -d\"",
            sh  => "export WRF_CHEM=1\nexport WRF_KPP=1\nexport FLEX_LIB_DIR=\@PREFIX\@/lib\n" .
                   "export FLEX=\@PREFIX\@/bin/flex\nexport YACC=\"\@PREFIX\@/bin/yacc -d\"",
            csh => "setenv WRF_CHEM 1\nsetenv WRF_KPP 1\nsetenv FLEX_LIB_DIR \@PREFIX\@/lib\n" .
                   "setenv FLEX \@PREFIX\@/bin/flex\nsetenv YACC \"\@PREFIX\@/bin/yacc -d\"",
        };
    }
    elsif ($typ eq "hydro") {
        $extras = { tcl => "setenv WRF_HYDRO 1", sh => "export WRF_HYDRO=1", csh => "setenv WRF_HYDRO 1" };
    }
    else {
        $extras = { tcl => "", sh => "", csh => "" };
    }
    if (chem_needs_modern_gcc($typ, $ver)) {
        my $fl = "-O3 -fPIC $MODERN_GCC_FLAGS";
        $extras->{tcl} .= sprintf("\nsetenv FFLAGS \"%s\"\nsetenv FCFLAGS \"%s\"", $fl, $fl);
        $extras->{sh}  .= sprintf("\nexport FFLAGS=\"%s\"\nexport FCFLAGS=\"%s\"", $fl, $fl);
        $extras->{csh} .= sprintf("\nsetenv FFLAGS \"%s\"\nsetenv FCFLAGS \"%s\"", $fl, $fl);
    }
    my %subs = ('@HOME@' => $S{home}, '@PREFIX@' => $S{prefix},
                '@WRFDIR@' => wrf_dir($typ, $ver), '@WPSDIR@' => wps_dir($typ, $ver),
                '@NPROCS@' => $S{nprocs}, '@VER@' => $ver, '@TYPE@' => $lab);
    for my $spec (["WRF$lab", $TCL_T, "tcl"], ["WRF$lab.sh", $SH_T, "sh"], ["WRF$lab.csh", $CSH_T, "csh"]) {
        my ($fname, $tmpl, $key) = @$spec;
        my $txt = $tmpl;
        $txt =~ s/\@EXTRA\@/$extras->{$key}/g;
        for my $k (keys %subs) {
            my $kk = quotemeta($k);
            my $v = $subs{$k};
            $txt =~ s/$kk/$v/g;
        }
        _spew($S{moddir} . "/" . $fname, $txt) unless $S{dryrun};
    }
    say("Environment files written for $lab", "\x{2713}", 0, 1, 6);
    return;
}

sub fire_guidance {
    my ($types) = @_;
    my @L;
    if (grep { $_ eq "fire" } @$types) {
        push @L, "", "WRF-Fire (real data) - build type 'Fire':",
             sprintf("  1. Put NFUEL_CAT in %s/landfire/ and ZSF in %s/highres_elev/ (geogrid format).", $S{geogdir}, $S{geogdir}),
             sprintf("  2. Create a domain:  %s --mkdomain NAME -fire", $EXE),
             sprintf("     (adds sr_x/sr_y=%d and a &fire block to namelist.input, subgrid_ratio_x/y to namelist.wps)", $FIRE_SR),
             "  3. Edit &fire: ifire=2 (level set), fire_fuel_read=-1 (fuel from WPS), ignition lines/times.",
             "     Grid settings in namelist.input must match namelist.wps; sr_x/sr_y = subgrid_ratio_x/y.";
    }
    if (grep { $_ eq "fireideal" } @$types) {
        push @L, "", "WRF-Fire (ideal cases) - build type 'FireIdeal' (compiled with ./compile em_fire):",
             "  cd <WRF-FireIdeal>/test/em_fire",
             "  ln -sf namelist.input_two_fires namelist.input ; ln -sf input_sounding_two_fires input_sounding",
             "  ./ideal.exe ; ./wrf.exe          (default case is hill_simple)";
    }
    return \@L;
}

sub write_dolist {
    my ($types) = @_;
    my $have_mod = (which("modulecmd") || -e "/usr/bin/modulecmd" || $ENV{MODULESHOME}) ? 1 : 0;
    my @L = ("Greetings, wonderful WRF owner.", "",
             "WRF was installed under: $S{home}",
             "Build type(s): " . join(", ", map { type_label($_) } @$types), "",
             "For you to use the system the environment must be set. Pick ONE build type",
             "at a time (WRFARW, WRFChem, WRFHydro, WRFFire or WRFFireIdeal) - do not load more than one together.", "");
    my $first = type_label($types->[0]);
    if ($have_mod) {
        push @L, "### Option 1: Environment Modules (recommended) ###", "",
             " Bash (add to ~/.bashrc):", "",
             "   export MODULEPATH=\"\${MODULEPATH}:$S{moddir}\"",
             "   module load WRF$first", "",
             " Tcsh (add to ~/.cshrc):", "",
             "   setenv MODULEPATH \${MODULEPATH}:$S{moddir}",
             "   module load WRF$first", "",
             "### Option 2: Source the environment file ###";
    }
    else {
        push @L, "The environment modules package (modulecmd) does not appear to be installed,",
             "so just source the environment files instead:";
    }
    push @L, "";
    for my $t (@$types) {
        my $lab = type_label($t);
        push @L, " WRF-$lab:",
             "   Bash : source $S{moddir}/WRF$lab.sh",
             "   Tcsh : source $S{moddir}/WRF$lab.csh";
    }
    push @L, @{ fire_guidance($types) };
    push @L, "", "And while you are editing your login file, comment out or delete references to",
         "previous WRF environments, because they will come looking for you.";
    my $text = join("\n", @L) . "\n";
    if (!$S{dryrun}) {
        _spew(expanduser("~/WRF-DoList.txt"), $text);
    }
    say("A special message from the WRF Overlord:", "*", 2, 1);
    print join("\n", map { "    " . $_ } @L), "\n";
    say("This information was also written to ~/WRF-DoList.txt", "\x{2713}", 1, 1);
    return;
}

sub print_cpu_info {
    my $i = $S{isys};
    info(sprintf("Processor information dug up for %s:", $i->{host}), 2);
    printf("      HOSTNAME         : %s\n      SOCKETS          : %s\n      CORES PER SOCKET : %s\n      TOTAL CORES      : %s\n",
           $i->{host}, $i->{sockets}, $i->{cores_per_socket}, $i->{total_cores});
    if ($i->{total_cores} < 2) {
        warnmsg(sprintf("Only %d core detected - WRF will be painfully slow. If that is wrong, set NPROCS in the module files after install.",
                        $i->{total_cores}));
    }
    if ($i->{ht}) {
        warnmsg("Hyper-threading appears to be ON. WRF runs most efficiently with one MPI rank per physical core;\n" .
                "using more will probably degrade performance (run_wrf uses WRF_NP ranks - default 8, change it if needed).");
    }
    return;
}

# =============================================================================
#  Flags / configuration (Build_WRF_ParseFlags + Build_WRF_Configure)
# =============================================================================
my @FLAG_HELP = (
    ["--install [REL]", "Fresh install of WRF/WPS + libraries. REL may be list|listall|listgeog."],
    ["-arw -chem -hydro -fire -fireideal -all", "Build types (or --type arw|chem|hydro|fire|fireideal, repeatable).\n                                 -fire = WRF-Fire real-data build + fire-ready WPS; -fireideal = em_fire ideal cases;\n                                 -all = arw, chem, hydro, fire."],
    ["--update [REL]", "Build a newer release next to the current one, reuse libraries, refresh domains."],
    ["--addpack LIST", "Install optional packages: domwiz, scripts, cdsapi."],
    ["--addgeog LIST", "Install terrestrial datasets: chem, mandatory, fire (creates landfire/ + highres_elev/ folders), or any UCAR dataset name."],
    ["--nogeog", "Do not download the base WPS_GEOG dataset."],
    ["--import DIR", "Import domains (dirs holding namelist.wps) from an older installation."],
    ["--scour", "Delete the previous release directories after a successful install/update."],
    ["--continue", "Resume: skip libraries already built."],
    ["--force", "Rebuild WRF/WPS even if present; allow installing an earlier release. Support libraries already installed are kept."],
    ["--force-libs", "Also rebuild the support libraries (zlib, hdf5, netcdf, mpich, ..., flex, byacc)."],
    ["--homedir DIR", "Top-level install directory (default \$WRF_HOME or ~/Build_WRF)."],
    ["--repodir DIR", "Take package tarfiles from a local repository instead of the network."],
    ["--nolocal", "Ignore tarfiles in <home>/packages and download fresh copies."],
    ["--nounpack", "Download only; do not unpack or build."],
    ["--scrubpkgs", "Delete package tarfiles after installation (default: keep them)."],
    ["--norefresh", "With --update, do not refresh existing domain directories."],
    ["--curl / --wget", "Force curl or wget for downloads (default: auto = wget, then curl, then built-in urllib)."],
    ["--dl-jobs N", "Parallel HTTP range connections per file (default 8; 1 = single stream). Big speed-up for\n                                 the WPS_GEOG/Chem tarballs and ECMWF data; --getdata is capped at 3 to stay polite."],
    ["--proxy HOST:PORT", "Set http_proxy/https_proxy (proxies are cleared by default)."],
    ["--wrf-opt N --wps-opt N", "Override the configure menu choices (defaults 34 and 3, gfortran dmpar)."],
    ["--allyes", "Answer YES to every question (for automation)."],
    ["--dryrun", "Show what would be done without downloading or building."],
    ["--mkdomain NAME", "Create a run domain under <home>/NAME (from setup_wrf_domain.sh); add -fire for a WRF-Fire domain."],
    ["--getdata gfs|ecmwf|era5", "Download initial/boundary data; see --date --cycle --start --end --step --margin --bbox --domain.\n                                 The area comes from the domain's namelist.wps (d01 lat/lon extent + --margin)."],
    ["--run auto|YYYYMMDD CC HH [gfs|ecmwf|era5]", "Run geogrid(opt)/ungrib/metgrid/real/wrf for --domain (from run_wrf.sh); source defaults to --source (gfs)."],
    ["--debug  --version  --flags  --help", "Housekeeping."],
);

sub print_flags {
    say("AVAILABLE FLAGS - BECAUSE YOU ASKED NICELY AND I'M BEGINNING TO LIKE YOU", "", 1, 1);
    for my $fd (@FLAG_HELP) {
        printf("    %-30s %s\n", $fd->[0], $fd->[1]);
    }
    print "\n";
    exit 1;
}

sub print_help {
    say(sprintf("USAGE:  %% %s [primary flag] [build type] [additional options]", $EXE), "", 1, 1);
    my $txt = <<'EOT';
    RUDIMENTARY INFORMATION OF THE BASIC KIND:

    To do a default installation of the current release:

       %% __EXE__ --install -arw                 (or -chem, -hydro, -fire, -all)

    To install WRF-Fire (real-data build, or ideal em_fire cases):

       %% __EXE__ --install -fire
       %% __EXE__ --install -fireideal
       %% __EXE__ --mkdomain firedom -fire

    To list the releases you can install:

       %% __EXE__ --install list

    To update an existing installation:

       %% __EXE__ --update

    To install an optional package or terrestrial dataset:

       %% __EXE__ --addpack domwiz,scripts,cdsapi
       %% __EXE__ --addgeog chem

    Daily-use helpers carried over from your automation scripts:

       %% __EXE__ --mkdomain ethiowrf
       %% __EXE__ --getdata gfs --domain ~/Build_WRF/ethiowrf --date 20261016 --end 72
       %% __EXE__ --run auto --domain ~/Build_WRF/ethiowrf

    For the full flag list:  %% __EXE__ --flags
EOT
    $txt =~ s/%%/%/g;
    $txt =~ s/__EXE__/$EXE/g;
    print $txt;
    exit 1;
}

# --- argparse-equivalent command line parsing --------------------------------
sub _optlike {   # argparse: tokens starting with "-", except "-" and negative numbers
    my ($t) = @_;
    return 0 if !defined $t;
    return 0 if $t eq "-";
    return 0 if $t =~ /^-\d+$/ || $t =~ /^-\d*\.\d+$/;
    return ($t =~ /^-/) ? 1 : 0;
}

sub _usage_error {
    my ($msg) = @_;
    print STDERR "$EXE: error: $msg\n";
    exit 2;
}

sub _take_val {  # a required option value (argparse "expected one argument")
    my ($argv, $inline, $optname) = @_;
    return $inline if defined $inline;
    if (@$argv && !_optlike($argv->[0])) { return shift @$argv; }
    _usage_error("argument $optname: expected one argument");
}

sub _check_int {
    my ($v, $optname) = @_;
    _usage_error("argument $optname: invalid int value: '$v'")
        unless defined $v && $v =~ /^[-+]?\d+$/;
    return $v;
}

sub _check_float {
    my ($v, $optname) = @_;
    _usage_error("argument $optname: invalid float value: '$v'")
        unless defined $v && $v =~ /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/;
    return $v;
}

sub _check_choice {
    my ($v, $optname, @choices) = @_;
    _usage_error("argument $optname: invalid choice: '$v' (choose from " .
                 join(", ", map { "'$_'" } @choices) . ")")
        unless defined $v && grep { $_ eq $v } @choices;
    return $v;
}

sub parse_args {
    my @argv = @ARGV;
    my %A = (
        install => undef, update => undef, addpack => [], addgeog => [],
        mkdomain => undef, getdata => undef, run => undef,
        types => [], types_long => [], all => 0,
        homedir => undef, repodir => undef, imports => [],
        allyes => 0, continue => 0, force => 0, nolocal => 0, nounpack => 0,
        scrubpkgs => 0, norefresh => 0, nogeog => 0, curl => 0, wget => 0,
        scour => 0, debug => 0, dryrun => 0, version => 0, flags => 0,
        force_libs => 0, proxy => undef,
        wrf_opt => "34", nest_opt => "1", wps_opt => "3",
        jobs => undef, dl_jobs => 8,
        domain => undef, date => undef, cycle => "00", start => 0, end => undef,
        step => 3, bbox => undef, margin => 1.0, outdir => undef, source => "gfs",
        help => 0,
    );
    my @extra;

    while (@argv) {
        my $t = shift @argv;

        if ($t eq "--") {                      # argparse separator: rest is positional
            push @extra, @argv;
            last;
        }
        if ($t eq "-h" || $t eq "--help") { $A{help} = 1; next; }

        if ($t =~ /^-([A-Za-z][A-Za-z0-9]*)$/) {
            my $short = $1;
            if ($short eq "all") { $A{all} = 1; next; }
            if (grep { $_ eq $short } @BUILD_TYPES) { push @{ $A{types} }, $short; next; }
            push @extra, $t;
            next;
        }

        my ($opt, $inline) = ($t, undef);
        if ($t =~ /^--([A-Za-z][A-Za-z0-9_-]*)(?:=(.*))?$/s) {
            $opt  = "--$1";
            my $name = $1;
            $name =~ tr/-/_/;
            $inline = $2;
            if ($name eq "install" || $name eq "update") {   # nargs="?", const="current"
                my $v;
                if (defined $inline) { $v = $inline; }
                elsif (@argv && !_optlike($argv[0])) { $v = shift @argv; }
                else { $v = "current"; }
                $A{$name} = $v;
                next;
            }
            if ($name eq "run") {                            # nargs="*"
                my @vals;
                push @vals, $inline if defined $inline;
                push @vals, shift @argv while (@argv && !_optlike($argv[0]));
                $A{run} = \@vals;
                next;
            }
            if ($name eq "bbox") {                           # nargs=4 type=float
                _usage_error("argument --bbox: expected one argument") if defined $inline;
                my @vals;
                for (1 .. 4) {
                    _usage_error("argument --bbox: expected 4 arguments")
                        unless (@argv && !_optlike($argv[0]));
                    push @vals, _check_float(shift @argv, "--bbox") + 0.0;
                }
                $A{bbox} = \@vals;
                next;
            }
            if ($name eq "allyes" || $name eq "continue" || $name eq "force"
                || $name eq "nolocal" || $name eq "nounpack" || $name eq "scrubpkgs"
                || $name eq "norefresh" || $name eq "nogeog" || $name eq "curl"
                || $name eq "wget" || $name eq "scour" || $name eq "debug"
                || $name eq "dryrun" || $name eq "version" || $name eq "flags"
                || $name eq "force_libs") {
                _usage_error("argument --$name: ignored explicit argument '$inline'") if defined $inline;
                $A{$name} = 1;
                next;
            }
            if ($name eq "addpack" || $name eq "addgeog" || $name eq "import" || $name eq "type") {
                my $v = _take_val(\@argv, $inline, "--$name");
                if ($name eq "type") {
                    _check_choice($v, "--type", @BUILD_TYPES);
                    push @{ $A{types_long} }, $v;
                }
                else {
                    my $dest = $name eq "import" ? "imports" : $name;
                    push @{ $A{$dest} }, $v;
                }
                next;
            }
            if ($name eq "homedir" || $name eq "repodir" || $name eq "proxy"
                || $name eq "domain" || $name eq "date" || $name eq "cycle"
                || $name eq "outdir" || $name eq "mkdomain") {
                $A{$name} = _take_val(\@argv, $inline, "--$name");
                next;
            }
            if ($name eq "wrf_opt" || $name eq "nest_opt" || $name eq "wps_opt") {
                $A{$name} = _take_val(\@argv, $inline, "--$name");
                next;
            }
            if ($name eq "jobs" || $name eq "dl_jobs" || $name eq "start"
                || $name eq "end" || $name eq "step") {
                $A{$name} = _check_int(_take_val(\@argv, $inline, "--$name"), "--$name") + 0;
                next;
            }
            if ($name eq "margin") {
                $A{margin} = _check_float(_take_val(\@argv, $inline, "--margin"), "--margin") + 0.0;
                next;
            }
            if ($name eq "getdata") {
                $A{getdata} = _check_choice(_take_val(\@argv, $inline, "--getdata"), "--getdata",
                                            qw(gfs ecmwf era5));
                next;
            }
            if ($name eq "source") {
                $A{source} = _check_choice(_take_val(\@argv, $inline, "--source"), "--source",
                                           qw(gfs ecmwf era5));
                next;
            }
            push @extra, $t;
            next;
        }

        # short options like -x, unknown multi-char shorts, bare words: unrecognized
        push @extra, $t;
    }
    return (\%A, \@extra);
}

sub configure {
    my ($args) = @_;
    $S{continue}   = $args->{continue};
    $S{allyes}     = $args->{allyes};
    $S{force}      = $args->{force};
    $S{force_libs} = $args->{force_libs};
    $S{nolocal}    = $args->{nolocal};
    $S{nounpack}   = $args->{nounpack};
    $S{scrubpkgs}  = $args->{scrubpkgs};
    $S{refresh}    = $args->{norefresh} ? 0 : 1;
    $S{nogeog}     = $args->{nogeog};
    $S{scour}      = $args->{scour};
    $S{debug}      = $args->{debug};
    $S{dryrun}     = $args->{dryrun};
    $S{wrf_opt}    = $args->{wrf_opt};
    $S{nest_opt}   = $args->{nest_opt};
    $S{wps_opt}    = $args->{wps_opt};
    $S{args}       = $args;
    # Proxy handling: OFF by default (Build_WRF), ON with --proxy
    delete $ENV{http_proxy};
    delete $ENV{https_proxy};
    if ($args->{proxy}) {
        my $pr = $args->{proxy};
        $pr =~ s{/+$}{};
        $pr .= "/";
        $ENV{http_proxy}  = "http://" . $pr;
        $ENV{https_proxy} = "https://" . $pr;
    }
    $ENV{LC_ALL} = "C";

    # download method
    if ($args->{curl} && $args->{wget}) {
        fatal("Pick one of --curl or --wget, not both.");
    }
    $S{method}  = $args->{curl} ? "curl" : ($args->{wget} ? "wget" : "auto");
    my $dlj = $args->{dl_jobs} // 8;
    $S{dl_jobs} = min(DL_MAX_JOBS, max(1, int($dlj)));

    # home
    my $home = $args->{homedir} // $ENV{WRF_HOME} // "";
    if (!$home) {                       # running from inside a domain (./wrfall ...): use its recorded home
        $home = domain_meta(getcwd())->{home} // "";
    }
    my $prev = expanduser("~/Build_WRF");
    if (!$home) {
        if (defined $args->{install} && !$args->{allyes}) {
            say("Finding a perfect home - Location, Location, ..., then Simulation", "*", 1, 1);
            $home = ask_value("Where would you like to install WRF?", $prev);
        }
        else {
            $home = $prev;
        }
    }
    $home =~ s{/+$}{};
    $home = $home ne "" ? $home : $prev;
    set_paths($home);

    # repository
    $S{repodir} = "";
    if ($args->{repodir}) {
        my $r = abspath(expanduser($args->{repodir}));
        if (!-d $r) {
            fatal(sprintf("You passed --repodir %s but that directory does not exist.", $args->{repodir}));
        }
        $S{repodir} = $r;
    }

    # build types
    my @types = (@{ $args->{types} }, @{ $args->{types_long} });
    @types = @ALL_TYPES if $args->{all};
    $S{types} = _dedup(@types);

    $S{isys}   = collect_system_info();
    $S{nprocs} = $S{isys}{total_cores} || cpu_count() || 4;
    $S{makej}  = $args->{jobs} || min(8, cpu_count() || 4);
    return;
}

sub sanity_check {
    my ($args) = @_;
    my @primary = grep { $_ } (defined $args->{install}, defined $args->{update});
    if (scalar(@primary) > 1) {
        fatal("This is not a \"McSloppy Burger\", so you can't have it \"Your Way!\"\n\n" .
              "It's either \"--install\" or \"--update\", but not both.");
    }
    my $machine = (POSIX::uname())[4];
    if ($machine eq "i386" || $machine eq "i686") {
        fatal("This installer targets 64-bit Linux. This system appears to be 32-bit.");
    }
    if ($^O ne "linux" && !$args->{dryrun}) {
        warnmsg("This installer is written for Linux; expect surprises on $^O.");
    }
    if (defined $args->{update} && (@{ $args->{addpack} } || @{ $args->{addgeog} })) {
        fatal("\"--addpack\" and \"--addgeog\" are stand-alone options; they can't be mixed with \"--update\".");
    }
    return;
}

# =============================================================================
#  --install
# =============================================================================
sub list_releases {
    my ($mode) = @_;
    my $inst = read_release($S{home});
    my $cur = $inst->{wrfems} // "";
    my $rels = available_releases();
    info(sprintf("Installed release : %s", $cur ne "" ? $cur : "No WRF installed"), 1);
    info("Summary of available WRF/WPS releases", 1);
    for my $i (0 .. $#$rels) {
        my $r = $rels->[$i];
        my $tag = ($i == 0) ? "  <- Most current" : "";
        $tag = "  <- Currently installed" if $r eq $cur;
        printf("        V%-8s%s\n", $r, $tag);
    }
    print "\n";
    if ($mode eq "listgeog" || $mode eq "listall") {
        info("Terrestrial datasets available", 1);
        printf("        %-28s %s\n", "geog_high_res_mandatory", "Base WPS_GEOG (installed by default)");
        for my $n (@CHEM_GEOGS) {
            printf("        %-28s %s\n", $n, "WRF-Chem dataset (group: chem)");
        }
        printf("        %-28s %s\n", "fire", "WRF-Fire folders landfire/ + highres_elev/ (you supply NFUEL_CAT, ZSF)");
    }
    if ($mode eq "listall") {
        info("Optional packages", 1);
        for my $k (@ADDPACK_ORDER) {
            printf("        %-10s %s\n", $k, $ADDPACKS{$k});
        }
    }
    print "\n";
    info(($cur eq $rels->[0]) ? "No need for an update at this time"
                              : "You now have the power - Abuse it wisely!", 0);
    exit 0;
}

sub resolve_release {
    my ($req) = @_;
    list_releases($req) if grep { $_ eq $req } qw(list listall listgeog);
    my $rels = available_releases();
    return $rels->[0] if $req eq "current";
    if ($req !~ /^\d+\.\d+(\.\d+)?$/) {
        fatal(sprintf("Fantasy Release: %s\n\nA release looks like %s.", $req, $DEFAULT_RELEASE));
    }
    return $req;
}

sub do_install {
    my ($args) = @_;
    my $types = $S{types};
    list_releases($args->{install}) if grep { $_ eq ($args->{install} // "") } qw(list listall listgeog);
    if (!@$types) {
        fatal(sprintf("You must choose at least one build type:\n" .
              "    For WRF-ARW   : %s --install -arw\n" .
              "    For WRF-Chem  : %s --install -chem\n" .
              "    For WRF-Hydro : %s --install -hydro\n" .
              "    For WRF-Fire  : %s --install -fire   (or -fireideal for em_fire ideal cases)\n" .
              "    Or all of them: %s --install -all", $EXE, $EXE, $EXE, $EXE, $EXE));
    }
    my $rel = resolve_release($args->{install});
    my $inst = read_release($S{home});
    my $irel = $inst->{wrfems} // "";
    my @already = grep {
        my $t = $_;
        (-f wrf_dir($t, $rel) . "/main/wrf.exe")
        && ((grep { $_ eq $t } @NO_WPS_TYPES) || -f wps_dir($t, $rel) . "/geogrid.exe")
    } @$types;
    if ($irel eq $rel && scalar(@already) == scalar(@$types) && !($S{force} || $S{continue})) {
        say(sprintf("Build_WRF-style notice: release %s (%s) is already installed.\n" .
                    "Pass \"--force\" to rebuild it or \"--continue\" to resume.",
                    $rel, join(", ", map { type_label($_) } @$types)), "!", 2, 1);
        return;
    }
    if ($irel ne "" && vt_cmp(_vt_num($rel), _vt_num($irel)) < 0 && !$S{force}) {
        fatal(sprintf("Installation Faux Paux: %s > %s\n\nYou are attempting to install an earlier release than the one that\n" .
              "currently resides here. If you are true about your intent, use \"--force\".", $irel, $rel));
    }

    info(sprintf("Preparing to install WRF/WPS V%s for: %s", $rel, join(", ", map { type_label($_) } @$types)), 1);
    check_prereqs();
    my $est = disk_estimate_gb($types, $S{nogeog}, (grep { $_ eq "chem" } @$types) ? 1 : 0);
    info(sprintf("Rough disk space needed: about %d GB (planning figure; check the dataset sizes you actually select).", $est), 1);
    my $parent = (-d $S{home}) ? $S{home} : (dirname($S{home}) || ".");
    my $free = eval { _disk_free_gb($parent) };
    if (defined $free && $free < $est) {
        warnmsg(sprintf("Only %.0f GB free on %s - that may not be enough.", $free, $parent));
    }
    print_cpu_info();
    info(format_system_info($S{isys}), 1);

    if (!ask("Installing WRF $rel. Is this OK by you?", "OK by me")) {
        say("Maybe we can try again later - Bye", "", 1, 1);
        return;
    }
    make_dirs();
    $S{stamp} = $rel;

    build_libraries();
    for my $t (@$types) {
        build_wrf($t, $rel);
        build_wps($t, $rel) unless grep { $_ eq $t } @NO_WPS_TYPES;
    }
    # geography first so the namelist path exists; reapply namelist path afterwards
    if (grep { my $t = $_; !grep { $_ eq $t } @NO_WPS_TYPES } @$types) {
        install_geog(undef, (grep { $_ eq "chem" } @$types) ? 1 : 0);
    }
    setup_fire_geog() if grep { $_ eq "fire" } @$types;
    if (!$S{nounpack}) {
        for my $t (@$types) {
            write_environment_files($t, $rel);
        }
        write_release($S{home}, $rel, $types) unless $S{dryrun};
        # Domain Wizard comes with the original installer
        if (!-d scripts_dir() . "/WRF-domain-Wizard") {
            do_addpack_domwiz(1);
        }
        write_dolist($types);
        import_domains($args->{imports}, $rel);
        if ($S{scour} && $irel ne "" && $irel ne $rel) {
            scour_old($irel, $inst->{types} // $types);
        }
    }
    info("Installation complete - Now go forth and simulate!", 2);
    for my $t (@$types) {
        if ($t eq "fireideal") {
            printf("      WRF-%s: %s/main/{wrf,ideal}.exe  (test/em_fire)\n", type_label($t), wrf_dir($t, $rel));
            next;
        }
        printf("      WRF-%s: %s/main/{wrf,real,ndown,tc}.exe\n", type_label($t), wrf_dir($t, $rel));
        printf("      WPS-%s: %s/{geogrid,ungrib,metgrid}.exe\n", type_label($t), wps_dir($t, $rel));
    }
    printf("\n      Environment files : %s\n      Logs              : %s\n\n", $S{moddir}, $S{logdir});
    return;
}

sub scour_old {
    my ($old, $types) = @_;
    info(sprintf("Scouring release %s (--scour)", $old), 1);
    for my $t (@$types) {
        rm(wrf_dir($t, $old));
        rm(wps_dir($t, $old));
    }
    return;
}

# =============================================================================
#  --update
# =============================================================================
sub do_update {
    my ($args) = @_;
    my $inst = read_release($S{home});
    my $irel = $inst->{wrfems} // "";
    if ($irel eq "") {
        fatal(sprintf("It appears that you are putting the cart before the horse. I am unable to find a current\n" .
              "installation under %s, which is necessary when using \"--update\".\n" .
              "Pass --homedir or set WRF_HOME.", $S{home}));
    }
    list_releases($args->{update}) if grep { $_ eq ($args->{update} // "") } qw(list listall);
    my $types = (@{ $S{types} }) ? $S{types}
              : ((@{ $inst->{types} // [] }) ? $inst->{types} : ["arw"]);
    my $target = resolve_release($args->{update});
    if (vt_cmp(_vt_num($target), _vt_num($irel)) <= 0 && !$S{force}) {
        say("No need to update - You are current (V$irel), and beautiful!", "\x{263A}", 2, 1);
        return;
    }
    info(sprintf("Updating V%s -> V%s for: %s", $irel, $target, join(", ", map { type_label($_) } @$types)), 1);
    info("Libraries are reused; the new release is built next to the old one.", 0);
    check_prereqs();
    if (!ask("Is this OK by you?", "OK by me")) {
        say("Maybe we can try again later - Bye", "", 1, 1);
        return;
    }
    make_dirs();
    build_libraries();                    # a no-op unless libraries are missing/--force
    for my $t (@$types) {
        build_wrf($t, $target);
        build_wps($t, $target) unless grep { $_ eq $t } @NO_WPS_TYPES;
        write_environment_files($t, $target) unless $S{nounpack};
    }
    return if $S{nounpack};
    write_release($S{home}, $target, $types);
    refresh_domains($target, $types) if $S{refresh};
    write_dolist($types);
    scour_old($irel, $types) if $S{scour};
    info("The system update is complete - Now go forth and simulate!", 2);
    return;
}

# =============================================================================
#  --addpack / --addgeog
# =============================================================================
sub split_args {
    my ($lst) = @_;
    my @out;
    for my $item (@$lst) {
        for my $x (split(/[,;\s]+/, (defined $item ? $item : ""))) {
            push @out, lc($x) if length $x;
        }
    }
    return \@out;
}

sub do_addpack_domwiz {
    my ($quiet) = @_;
    my $dest = scripts_dir() . "/WRF-domain-Wizard";
    make_path(scripts_dir()) unless $S{dryrun};
    if (-d "$dest/.git" && !$S{force}) {
        say("Domain Wizard already installed", "\x{2713}", 0, 1) unless $quiet;
        return;
    }
    if (!which("git")) {
        warnmsg("git not found - skipping Domain Wizard download");
        return;
    }
    info("Downloading WRF Domain Wizard", 1);
    rm($dest);
    run(cmd => ["git", "clone", "--depth", "1", $DOMWIZ_REPO, $dest], check => 0);
    return;
}

sub do_addpack_scripts {
    my $d = scripts_dir();
    make_path($d) unless $S{dryrun};
    info(sprintf("Saving your original automation scripts in %s", $d), 1);
    for my $f (@UTIL_SCRIPTS) {
        my $dest = "$d/$f";
        if (-f $dest && !$S{force}) {
            say(sprintf("%-30s found locally", $f), "\x{2713}", 0, 1, 6);
            next;
        }
        acquire(sprintf($UTIL_RAW, $f), $dest);
        chmod(0755, $dest) unless $S{dryrun};
        say(sprintf("%-30s downloaded", $f), "\x{2713}", 0, 1, 6);
    }
    return;
}

sub _python_exe {
    return which("python3") || which("python") || "";
}

sub do_addpack_cdsapi {
    info("Installing the cdsapi Python module (pip --user)", 1);
    my $py = _python_exe();
    if ($py ne "") {
        run(cmd => [$py, "-m", "pip", "install", "--user", "cdsapi"], check => 0);
    }
    else {
        warnmsg("python3/python not found - cannot install the cdsapi module (install Python 3 and rerun)");
    }
    if (!-f expanduser("~/.cdsapirc")) {
        warnmsg("ERA5 downloads also need your CDS credentials in ~/.cdsapirc\n" .
                "(see the Copernicus Climate Data Store 'How to API' page).");
    }
    return;
}

sub do_addpack {
    my ($args) = @_;
    my $packs = split_args($args->{addpack});
    my $geogs = split_args($args->{addgeog});
    my $inst = read_release($S{home});
    if (!%$inst && !$S{dryrun}) {
        fatal(sprintf("I'm unable to determine the release of the current installation under %s,\n" .
              "which is needed when processing \"--addpack\"/\"--addgeog\". Pass --homedir or set WRF_HOME.",
              $S{home}));
    }
    make_dirs();
    my @todo;
    for my $p (@$packs) {
        $p = $ADDPACK_ALIASES{$p} // $p;
        if (!$ADDPACKS{$p}) {
            fatal(sprintf("Unknown package \"%s\". Available: %s", $p, join(", ", @ADDPACK_ORDER)));
        }
        push @todo, $p;
    }
    if (@todo) {
        my @uniq = @{ _dedup(@todo) };
        info(sprintf("Installing %d optional package(s): %s", scalar(@uniq), join(", ", @uniq)), 1);
        for my $p (@uniq) {
            if    ($p eq "domwiz")  { do_addpack_domwiz(0); }
            elsif ($p eq "scripts") { do_addpack_scripts(); }
            elsif ($p eq "cdsapi")  { do_addpack_cdsapi(); }
        }
    }
    if (@$geogs) {
        my @names;
        for my $g (@$geogs) {
            push @names, @{ $ADDGEOG_GROUPS{$g} // [$g] };
        }
        @names = @{ _dedup(@names) };
        my %known = map { $_ => 1 } (@CHEM_GEOGS, "geog_high_res_mandatory");
        my @unk = grep { !$known{$_} } @names;
        if (@unk) {
            warnmsg(sprintf("Not in my built-in list, will try %s/<name>.tar.bz2: %s",
                            $GEOG_BASE, join(", ", @unk)));
        }
        install_geog(\@names, 0) if @names;
        setup_fire_geog() if grep { $_ eq "fire" } @$geogs;
    }
    info("Optional package installation complete", 1);
    return;
}

# =============================================================================
#  Domains: import / refresh / create
# =============================================================================
sub find_domains {
    my ($root) = @_;
    my @out;
    $root = abspath(expanduser($root));
    return @out unless -d $root;
    return ($root) if (-f "$root/namelist.wps" && -f "$root/namelist.input");
    opendir(my $dh, $root) or return @out;
    for my $d (sort readdir($dh)) {
        next if $d eq "." || $d eq "..";
        my $p = "$root/$d";
        if (-d $p && -f "$p/namelist.wps" && -f "$p/namelist.input") {
            push @out, $p;
        }
    }
    closedir($dh);
    return @out;
}

sub relink_domain {
    # Point the executables in a domain at the given release (setup_wrf_domain.sh behaviour).
    my ($dom, $typ, $ver) = @_;
    my $wps = wps_dir($typ, $ver);
    my $run_dir = wrf_dir($typ, $ver) . "/run";
    my @srcs = (glob("$wps/*.exe"), "$wps/link_grib.csh", glob("$run_dir/*.exe"));
    for my $src (@srcs) {
        next unless -e $src;
        my $dst = $dom . "/" . basename($src);
        unlink($dst) if lexists_p($dst);
        symlink($src, $dst);
    }
    for my $sub (qw(geogrid ungrib metgrid)) {
        my $s = "$wps/$sub";
        my $d = "$dom/$sub";
        if (-d $s) {
            rm($d);
            copy_tree($s, $d);
        }
    }
    # link everything else from run/ that the domain doesn't have (cp -as in the original)
    return unless -d $run_dir;
    opendir(my $dh, $run_dir) or return;
    while (my $ent = readdir($dh)) {
        next if $ent eq "." || $ent eq "..";
        my $d = "$dom/$ent";
        next if lexists_p($d);
        symlink("$run_dir/$ent", $d);
    }
    closedir($dh);
    return;
}

sub _json_write {
    my ($path, $ref) = @_;
    return if $S{dryrun};
    my $j = JSON::PP->new->canonical(1);
    _spew($path, $j->encode($ref) . "\n");
    return;
}

sub _json_read {
    my ($path) = @_;
    return undef unless -f $path;
    my $t = _slurp($path);
    return undef unless defined $t && length $t;
    return eval { JSON::PP->new->decode($t) };
}

sub refresh_domains {
    my ($ver, $types) = @_;
    my @doms = grep { -f "$_/.wrfems_domain" } find_domains($S{home});
    return unless @doms;
    info(sprintf("Refreshing %d run-time domain(s)", scalar(@doms)), 1);
    for my $d (@doms) {
        my $meta = _json_read("$d/.wrfems_domain") // {};
        my $typ = $meta->{type} // $types->[0];
        if (!grep { $_ eq $typ } @$types) {
            $typ = $types->[0];
        }
        my $stamp = POSIX::strftime("%Y%m%d%H%M%S", gmtime());
        for my $nl (qw(namelist.wps namelist.input)) {
            if (-f "$d/$nl") {
                copy2("$d/$nl", sprintf("%s/%s.%s.bak", $d, $nl, $stamp));
            }
        }
        if (!$S{dryrun}) {
            relink_domain($d, $typ, $ver);
            set_namelist_text("$d/namelist.wps", "geog_data_path", "'$S{geogdir}/'", 1);
            $meta->{version} = $ver;
            _json_write("$d/.wrfems_domain", $meta);
        }
        say(sprintf("Refreshed %s (your namelists were kept; backups saved as *.bak)", basename($d)),
            "\x{2713}", 0, 1, 6);
    }
    return;
}

sub import_domains {
    my ($imports, $ver) = @_;
    my @cand;
    for my $i (@$imports) {
        push @cand, find_domains($i);
    }
    return unless @cand;
    info(sprintf("Importing %d requested run-time domain(s) into %s", scalar(@cand), $S{home}), 1);
    my $inst = read_release($S{home});
    my $types = (@{ $inst->{types} // [] }) ? $inst->{types} : ["arw"];
    for my $src (@{ _dedup(@cand) }) {
        my $name = basename($src);
        my $dst = $S{home} . "/" . $name;
        if (-e $dst) {
            warnmsg(sprintf("Not imported: domain \"%s\" already exists under %s", $name, $dst));
            next;
        }
        if (run(cmd => ["rsync", "-aq", "$src/", "$dst/"], check => 0) != 0) {
            warnmsg("Import failed for $src");
            next;
        }
        if (!$S{dryrun}) {
            _json_write("$dst/.wrfems_domain", { type => $types->[0], version => $ver });
            relink_domain($dst, $types->[0], $ver);
            set_namelist_text("$dst/namelist.wps", "geog_data_path", "'$S{geogdir}/'", 1);
        }
        say("Mission \"Import $name\" Accomplished!", "\x{2713}", 0, 1, 6);
    }
    return;
}

my %SRC_DIRS = (gfs => "GFS_Input", ecmwf => "ECMWF_Input", era5 => "ERA5_Input");
my %VTABLES = (gfs => "Vtable.GFS", ecmwf => "Vtable.ECMWF", era5 => "Vtable.ERA-interim.pl");
my @OUT_SUBS = qw(metgrid ungrib Plots geogrid);   # <domain>/wrf_output/<sub>

sub domain_meta {
    my ($dom) = @_;
    my $m = _json_read("$dom/.wrfems_domain");
    return (ref($m) eq "HASH") ? $m : {};
}

sub resolve_domains {
    # --domain a,b,c  (names under <home>, relative or absolute paths).
    # With no --domain, the current directory is used when it is a domain
    # (this is what makes './wrfall --getdata gfs ...' work from inside a domain).
    my ($spec) = @_;
    my @out;
    my @names = grep { length $_ } split(/[,;\s]+/, (defined $spec ? $spec : ""));
    if (!@names) {
        return [getcwd()] if -f getcwd() . "/.wrfems_domain";
        return [];
    }
    for my $nm (@names) {
        my $p = abspath(expanduser($nm));
        if (!-d $p) {
            my $alt = $S{home} . "/" . $nm;
            if (-d $alt) { $p = $alt; }
        }
        push @out, $p;
    }
    return _dedup(@out);
}

sub date_dir {
    my ($dom, $source, $date, $cyc) = @_;
    # <domain>/GFS_Input/YYYYMMDDCC/  (note: a real sub-directory, not a name prefix).
    return sprintf("%s/%s/%s%s", $dom, $SRC_DIRS{$source}, $date, $cyc);
}

sub make_domain_dirs {
    my ($dom) = @_;
    make_path("$dom/$_") for values %SRC_DIRS;
    make_path("$dom/wrf_output/$_") for @OUT_SUBS;
    return;
}

sub scripts_dir {
    # $Build_WRF/Scripts : one shared folder for all domains.
    return $S{home} . "/Scripts";
}

sub _find_files {
    my ($root, $re) = @_;
    require File::Find;
    my @out;
    File::Find::find({
        wanted => sub {
            my $n = $File::Find::name;
            return unless -f $n;
            push @out, $n if $n =~ $re;
        },
        no_chdir => 1,
    }, $root);
    return \@out;
}

sub setup_scripts {
    # <home>/Scripts gets *.sh and *.py from Automation-Utility-Scripts and a clone of
    # WRF-domain-Wizard (Scripts/WRF-domain-Wizard). Created once, shared by every domain.
    my $sd = scripts_dir();
    make_path($sd) unless $S{dryrun};
    my @have = grep { -f "$sd/$_" } @UTIL_SCRIPTS;
    if (scalar(@have) == scalar(@UTIL_SCRIPTS) && !$S{force}) {
        say("$sd already filled", "\x{2713}", 0, 1, 6);
    }
    else {
        info("Filling $sd", 0);
        my $got = 0;
        if (which("git") && !$S{dryrun}) {
            my $tmp = _mkdtemp(".utils_XXXXXX", $sd);
            if ($tmp) {
                my $rc = run(cmd => ["git", "clone", "--depth", "1", $UTIL_REPO, "$tmp/r"],
                             check => 0, cleanup_sub => sub { rm($tmp); });
                if ($rc == 0) {
                    for my $pat (qr/\.sh\z/, qr/\.py\z/) {
                        for my $f (@{ _find_files("$tmp/r", $pat) }) {
                            my $dst = $sd . "/" . basename($f);
                            copy2($f, $dst);
                            chmod(0755, $dst);
                            $got = 1;
                        }
                    }
                }
                rm($tmp);
            }
        }
        elsif ($S{dryrun}) {
            say(sprintf("would git clone %s and copy *.sh/*.py into %s", $UTIL_REPO, $sd), "dbg", 0, 1, 6);
            $got = 1;
        }
        if (!$got) {                                   # no git / clone failed: raw files
            for my $f (@UTIL_SCRIPTS) {
                my $dest = "$sd/$f";
                next if (-f $dest && !$S{force});
                my $how = acquire(sprintf($UTIL_RAW, $f), $dest, 1);
                if (!defined $how) {
                    warnmsg("Could not fetch $f");
                    next;
                }
                chmod(0755, $dest) unless $S{dryrun};
            }
        }
    }
    do_addpack_domwiz(1);
    return;
}

sub link_wrfall {
    # Soft link <domain>/wrfall -> this script, so it can be run from the domain.
    my ($dom) = @_;
    require Cwd;
    my $me = Cwd::realpath($0);
    my $dst = "$dom/wrfall";
    unlink($dst) if lexists_p($dst);
    symlink($me, $dst);
    return;
}

sub detect_builds {
    # Scan <home> for finished builds: WPS-<ver>-<Label>/geogrid.exe + WRF-<ver>-<Label>/main/wrf.exe.
    # Returns {type: [versions, newest first]}. Works even when builds were made one at a time,
    # at different versions (e.g. Chem/Hydro 4.6.0 and Fire 4.7.0), or without a matching .release entry.
    my %label2typ = map { $TYPE_LABELS{$_} => $_ } keys %TYPE_LABELS;
    my %out;
    for my $d (glob($S{home} . "/WPS-*-*")) {
        my $b = basename($d);
        next unless $b =~ /^WPS-(\d+(?:\.\d+)*)-(\w+)$/;
        my ($ver, $lab) = ($1, $2);
        next unless exists $label2typ{$lab};
        my $typ = $label2typ{$lab};
        next if grep { $_ eq $typ } @NO_WPS_TYPES;
        next unless -f "$d/geogrid.exe" && -f wrf_dir($typ, $ver) . "/main/wrf.exe";
        push @{ $out{$typ} }, $ver;
    }
    for my $t (keys %out) {
        my @sorted = sort { vt_cmp(_vt_num($b), _vt_num($a)) } @{ $out{$t} };
        $out{$t} = \@sorted;
    }
    return \%out;
}

sub pick_build {
    # Choose (type, version) for --mkdomain: the type given on the command line, else the only
    # build that exists. With several builds and no type flag, list them and stop.
    my ($built, $inst) = @_;
    my $wanted = $S{types};
    if (@$wanted && grep { $_ eq $wanted->[0] } @NO_WPS_TYPES) {
        fatal("-$wanted->[0] has no WPS; run its ideal cases from <WRF dir>/test/em_fire. Use -fire for real-data domains.");
    }
    my $typ;
    if (@$wanted) {
        $typ = $wanted->[0];
    }
    elsif (scalar(keys %$built) == 1) {
        ($typ) = keys %$built;
    }
    elsif (scalar(keys %$built) > 1) {
        my @lines;
        for my $t (@BUILD_TYPES) {
            push @lines, sprintf("      -%-6s  (WRF/WPS %s)", $t, $built->{$t}[0]) if $built->{$t};
        }
        my $example = (sort keys %$built)[0];
        fatal(sprintf("More than one build is installed under %s:\n%s\n" .
                      "    Tell me which one this domain is for, e.g.:  %s --mkdomain %s -%s",
                      $S{home}, join("\n", @lines), $EXE, $S{mkdomain_name}, $example));
    }
    else {
        $typ = "arw";
    }
    if (!$built->{$typ}) {
        my $have = join(", ", map { sprintf("-%s (%s)", $_, $built->{$_}[0]) } sort keys %$built) || "nothing";
        fatal(sprintf("%s has not been built. Install it first.\n    Builds found under %s: %s",
                      type_label($typ), $S{home}, $have));
    }
    my $rel = $inst ? $inst->{wrfems} : undef;
    my $ver = (defined $rel && grep { $_ eq $rel } @{ $built->{$typ} }) ? $rel : $built->{$typ}[0];
    return ($typ, $ver);
}

sub do_mkdomain {
    # Port of setup_wrf_domain.sh (does NOT edit ~/.bashrc; writes <domain>/domain_env.sh instead).
    my ($args) = @_;
    my $inst = read_release($S{home});
    $S{mkdomain_name} = $args->{mkdomain};
    my $built = detect_builds();                 # {type: [versions, newest first]} found on disk
    if (!%$inst && !%$built) {
        fatal(sprintf("No installation found under %s. Run --install first (or pass --homedir).", $S{home}));
    }
    my ($typ, $ver) = pick_build($built, $inst);
    my $name = $args->{mkdomain};
    if ($name !~ /^[A-Za-z0-9._-]+$/) {
        fatal("Domain names may only contain letters, digits, dot, dash and underscore.");
    }
    my $dom = $S{home} . "/" . $name;
    if (!-f wps_dir($typ, $ver) . "/geogrid.exe") {
        fatal(sprintf("%s %s has not been built. Install it first.", type_label($typ), $ver));
    }
    make_path($dom);
    my $wps = wps_dir($typ, $ver);
    my $rdir = wrf_dir($typ, $ver) . "/run";
    relink_domain($dom, $typ, $ver);
    for my $spec (["namelist.wps", $wps], ["namelist.input", $rdir]) {
        my ($nl, $srcd) = @$spec;
        my $d = "$dom/$nl";
        unlink($d) if -l $d;
        if (!-f $d) {
            copy2("$srcd/$nl", $d);
        }
    }
    make_domain_dirs($dom);
    fire_prepare_domain($dom, $rdir) if $typ eq "fire";
    _json_write("$dom/.wrfems_domain", { type => $typ, version => $ver, home => $S{home} });
    _spew("$dom/domain_env.sh",
          sprintf("export WRF_HOME=\"%s\"\nexport WRF_WPS=\"%s\"\nexport WRF_RUN=\"%s\"\nexport DOMAIN_DIR=\"%s\"\n",
                  $S{home}, $wps, $rdir, $dom));
    link_wrfall($dom);
    setup_scripts();
    okmsg("Domain ready: $dom");
    require Cwd;
    printf("      Layout: GFS_Input/ ECMWF_Input/ ERA5_Input/ (each holds YYYYMMDDCC/), wrf_output/{metgrid,ungrib,Plots,geogrid}/,\n" .
           "              wrfall -> %s\n" .
           "      Shared scripts (utility scripts + WRF-domain-Wizard): %s\n" .
           "      Edit namelist.wps first (e.g. with WRF-domain-Wizard): --getdata reads its lat/lon extent\n" .
           "      and downloads only that area (+ --margin degrees).\n" .
           "      Work from inside the domain:\n" .
           "        cd %s\n" .
           "        ./wrfall --getdata gfs --date YYYYMMDD --cycle 00 --end 72\n" .
           "        ./wrfall --run auto gfs\n" .
           "      Load its variables with:  source %s/domain_env.sh\n",
           Cwd::realpath($0), scripts_dir(), $dom, $dom);
    return;
}

# =============================================================================
#  --getdata  (download_025_gfs.sh / download_ecmwf_ifs_025.sh / download_era5.*)
# -----------------------------------------------------------------------------
#  Domain extent from namelist.wps  (min/max latitude and longitude of d01)
# -----------------------------------------------------------------------------
my $_R_EARTH = 6370000.0;          # WPS uses a sphere of this radius
my $PI = 4 * atan2(1, 1);

sub _rad { my ($x) = @_; return $x * $PI / 180.0; }
sub _deg { my ($x) = @_; return $x * 180.0 / $PI; }
sub _atan { my ($x) = @_; return atan2($x, 1); }
sub _tan  { my ($x) = @_; return sin($x) / cos($x); }

sub nl_values {
    # First &geogrid/&share line 'key = a, b, ...' -> list of strings (quotes/comments stripped).
    my ($path, $key) = @_;
    open(my $fh, "<", $path) or return [];
    my $pat = qr/^\s*\Q$key\E\s*=\s*(.*?)\s*(?:!.*)?$/i;
    while (my $ln = <$fh>) {
        chomp $ln;
        next unless $ln =~ $pat;
        close($fh);
        my @vals;
        for my $v (split(/,/, $1)) {
            my $t = $v;
            $t =~ s/^\s+|\s+$//g;
            $t =~ s/^['"]|['"]$//g;
            push @vals, $t if length $t;
        }
        return \@vals;
    }
    close($fh);
    return [];
}

sub _wrap180 {
    my ($x) = @_;
    my $w = POSIX::fmod($x + 180.0, 360.0);
    $w += 360.0 if $w < 0;
    return $w - 180.0;
}

sub namelist_extent {
    # Return {minlat, maxlat, minlon, maxlon, proj} for domain 1 of a namelist.wps,
    # using the same map projections as WPS (lambert, polar, mercator, lat-lon).
    my ($nl_path) = @_;

    my $num = sub {
        my ($key, $default) = @_;
        my $v = nl_values($nl_path, $key);
        if (@$v && $v->[0] =~ /^\s*[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?\s*$/) {
            return $v->[0] + 0.0;
        }
        if (!defined $default) {
            die sprintf("'%s' not found in %s\n", $key, $nl_path);
        }
        return $default + 0.0;
    };

    my $mp = nl_values($nl_path, "map_proj");
    my $proj = @$mp ? lc($mp->[0]) : "lambert";
    my $ref_lat = $num->("ref_lat");
    my $ref_lon = $num->("ref_lon");
    my $t1 = $num->("truelat1", $ref_lat);
    my $t2 = $num->("truelat2", $t1);
    my $stand = $num->("stand_lon", $ref_lon);
    my $dx = $num->("dx");
    my $dy = $num->("dy", $dx);
    my $nx = int($num->("e_we")) - 1;                # mass-grid cells
    my $ny = int($num->("e_sn")) - 1;
    my $ref_x = $num->("ref_x", ($nx + 1) / 2.0);    # grid index (1 = first mass point)
    my $ref_y = $num->("ref_y", ($ny + 1) / 2.0);
    if ($proj eq "lat-lon" && abs($num->("pole_lat", 90.0) - 90.0) > 1e-6) {
        warnmsg("namelist.wps uses a rotated lat-lon pole; extent is approximate");
    }

    my $R = $_R_EARTH;
    my ($fwd, $inv);
    if ($proj eq "lambert") {
        my $p1 = _rad($t1);
        my $p2 = _rad($t2);
        my $n;
        if (abs($t1 - $t2) < 1e-9) {
            $n = sin($p1);
        }
        else {
            $n = log(cos($p1) / cos($p2)) / log(_tan($PI / 4 + $p2 / 2) / _tan($PI / 4 + $p1 / 2));
        }
        my $F = cos($p1) * (_tan($PI / 4 + $p1 / 2) ** $n) / $n;
        my $rho = sub {
            my ($phi) = @_;
            return $R * $F / (_tan($PI / 4 + $phi / 2) ** $n);
        };
        my $rho0 = $R * $F;
        $fwd = sub {
            my ($lat, $lon) = @_;
            my $dl = _rad(_wrap180($lon - $stand));
            return ($rho->(_rad($lat)) * sin($n * $dl),
                    $rho0 - $rho->(_rad($lat)) * cos($n * $dl));
        };
        $inv = sub {
            my ($x, $y) = @_;
            my $sg = $n > 0 ? 1.0 : -1.0;
            my $r = $sg * sqrt($x * $x + ($rho0 - $y) ** 2);
            die "degenerate lambert projection (r = 0)\n" if $r == 0;
            my $th = atan2($sg * $x, $sg * ($rho0 - $y));
            return (_deg(2 * _atan(($R * $F / $r) ** (1.0 / $n)) - $PI / 2),
                    _wrap180($stand + _deg($th / $n)));
        };
    }
    elsif ($proj eq "polar") {
        my $north = ($t1 >= 0) ? 1 : 0;
        my $k = $R * (1 + sin(_rad(abs($t1))));
        $fwd = sub {
            my ($lat, $lon) = @_;
            my $ph = _rad($north ? $lat : -$lat);
            my $r = $k * _tan($PI / 4 - $ph / 2);
            my $dl = _rad(_wrap180($lon - $stand));
            return ($r * sin($dl), $north ? (-$r * cos($dl)) : ($r * cos($dl)));
        };
        $inv = sub {
            my ($x, $y) = @_;
            my $r = sqrt($x * $x + $y * $y);
            my $ph = $PI / 2 - 2 * _atan($r / $k);
            return (($north ? _deg($ph) : -_deg($ph)),
                    _wrap180($stand + _deg(atan2($x, $north ? -$y : $y))));
        };
    }
    elsif ($proj eq "mercator") {
        my $c = cos(_rad($t1));
        $fwd = sub {
            my ($lat, $lon) = @_;
            return ($R * $c * _rad(_wrap180($lon - $stand)),
                    $R * $c * log(_tan($PI / 4 + _rad($lat) / 2)));
        };
        $inv = sub {
            my ($x, $y) = @_;
            return (_deg(2 * _atan(exp($y / ($R * $c))) - $PI / 2),
                    _wrap180($stand + _deg($x / ($R * $c))));
        };
    }
    elsif ($proj eq "lat-lon") {
        $fwd = sub { my ($lat, $lon) = @_; return (_rad($lon) * $R, _rad($lat) * $R); };
        $inv = sub { my ($x, $y) = @_; return (_deg($y / $R), _deg($x / $R)); };
        $dx = _rad($dx) * $R;
        $dy = _rad($dy) * $R;
    }
    else {
        die sprintf("unsupported map_proj '%s'\n", $proj);
    }

    my ($xr, $yr) = $fwd->($ref_lat, $ref_lon);
    my $x0 = $xr - ($ref_x - 0.5) * $dx;
    my $x1 = $xr + ($nx + 0.5 - $ref_x) * $dx;       # outer cell edges
    my $y0 = $yr - ($ref_y - 0.5) * $dy;
    my $y1 = $yr + ($ny + 0.5 - $ref_y) * $dy;
    my @pts;
    for my $i (0 .. 100) {                            # sample the whole boundary
        my $f = $i / 100.0;
        my ($xs, $ys) = ($x0 + $f * ($x1 - $x0), $y0 + $f * ($y1 - $y0));
        push @pts, [$inv->($xs, $y0)], [$inv->($xs, $y1)], [$inv->($x0, $ys)], [$inv->($x1, $ys)];
    }
    my @lats = map { $_->[0] } @pts;
    my @lons = map { $_->[1] } @pts;
    if (max(@lons) - min(@lons) > 180) {              # crosses +/-180
        @lons = map {
            my $m = POSIX::fmod($_, 360.0);
            $m += 360.0 if $m < 0;
            $m;
        } @lons;
    }
    return { minlat => min(@lats), maxlat => max(@lats),
             minlon => min(@lons), maxlon => max(@lons), proj => $proj };
}

sub download_bbox {
    # (W, E, S, N, text) for --getdata: namelist.wps extent + margin, snapped outward to 0.25 deg.
    my ($dom, $margin) = @_;
    my $nl = "$dom/namelist.wps";
    my $ext = namelist_extent($nl);
    my $q = sub {
        my ($v, $f) = @_;
        return $f->($v / 0.25) * 0.25;
    };
    my $w = $q->($ext->{minlon} - $margin, \&POSIX::floor);
    my $e = $q->($ext->{maxlon} + $margin, \&POSIX::ceil);
    my $s = max(-90.0, $q->($ext->{minlat} - $margin, \&POSIX::floor));
    my $n = min(90.0, $q->($ext->{maxlat} + $margin, \&POSIX::ceil));
    my $txt = sprintf("Domain extent (namelist.wps, d01, %s): lat %.2f to %.2f, lon %.2f to %.2f\n" .
                      "Download area (+%.2f deg margin): lat %.2f to %.2f, lon %.2f to %.2f",
                      $ext->{proj}, $ext->{minlat}, $ext->{maxlat}, $ext->{minlon}, $ext->{maxlon},
                      $margin, $s, $n, $w, $e);
    return ($w, $e, $s, $n, $txt);
}

sub fetch_binary {
    # Download one GRIB file: streamed to disk (no whole-file buffering),
    # resumable, parallel ranges capped at 3 (NOMADS throttles aggressive clients).
    my ($url, $out, $tries) = @_;
    $tries //= 5;
    my $part = $out . ".part";
    my $jobs = $S{dl_jobs} // 8;
    $jobs = 3 if $jobs > 3;
    $jobs = 1 if $jobs < 1;
    for my $n (1 .. $tries) {
        my $server_ok = 0;
        my $is_grib = 0;
        eval {
            if (_download_urllib($url, $part, $jobs) == 0 && -f $part) {
                $server_ok = 1;
                open(my $fh, "<:raw", $part) or die "cannot open $part: $!\n";
                my $magic;
                read($fh, $magic, 4);
                close($fh);
                $magic = "" unless defined $magic;
                $is_grib = ($magic eq "GRIB") ? 1 : 0;
            }
            1;
        };
        if ($server_ok) {
            if ($is_grib) {
                print "\n" if -t STDOUT;
                rename($part, $out);
                return 1;
            }
            rm($part);
            return 0;      # server answered, but no GRIB: not available yet
        }
        sleep(5 * $n);
    }
    return 0;
}

my @ERA5_SFC_VARS = qw(10m_u_component_of_wind 10m_v_component_of_wind 2m_dewpoint_temperature
                       2m_temperature land_sea_mask mean_sea_level_pressure sea_ice_cover
                       sea_surface_temperature skin_temperature snow_depth soil_type
                       surface_pressure volumetric_soil_water_layer_1 volumetric_soil_water_layer_2
                       volumetric_soil_water_layer_3 volumetric_soil_water_layer_4
                       soil_temperature_level_1 soil_temperature_level_2
                       soil_temperature_level_3 soil_temperature_level_4);
my @ERA5_PL_VARS = qw(geopotential relative_humidity specific_humidity temperature
                      u_component_of_wind v_component_of_wind);
my @ERA5_LEVELS = qw(1 2 3 5 7 10 20 30 50 70 100 125 150 175 200
                     225 250 300 350 400 450 500 550 600 650 700 750 775
                     800 825 850 875 900 925 950 975 1000);

sub _utc_now_ymd {
    return POSIX::strftime("%Y%m%d", gmtime());
}

sub getdata_one {
    my ($args, $dom) = @_;
    my $date = $args->{date} // _utc_now_ymd();
    my $cyc = $args->{cycle};
    my $end = defined $args->{end} ? $args->{end} : (($args->{getdata} eq "era5") ? 21 : 120);
    my ($w, $e, $s, $n, $txt);
    if ($args->{bbox}) {
        ($w, $e, $s, $n) = @{ $args->{bbox} };
        info(sprintf("Download area from --bbox: lat %.2f to %.2f, lon %.2f to %.2f", $s, $n, $w, $e), 1);
    }
    elsif ($dom && -f "$dom/namelist.wps") {
        eval {
            ($w, $e, $s, $n, $txt) = download_bbox($dom, $args->{margin});
            1;
        } or do {
            my $ex = $@ // "unknown error";
            $ex =~ s/\s+\z//;
            fatal(sprintf("Could not read the domain extent from %s/namelist.wps: %s\n(or pass --bbox W E S N)",
                          $dom, $ex));
        };
        info($txt, 1);
    }
    else {
        ($w, $e, $s, $n) = (20.0, 54.0, -10.0, 25.0);
        warnmsg(sprintf("No domain namelist.wps found - using the default area lat %.0f to %.0f, lon %.0f to %.0f\n" .
                        "(use --domain DIR or --bbox W E S N)", $s, $n, $w, $e), 1);
    }
    my $out;
    if ($args->{outdir}) {
        $out = abspath(expanduser($args->{outdir}));
    }
    elsif ($dom) {
        $out = date_dir($dom, $args->{getdata}, $date, $cyc);   # <domain>/GFS_Input/YYYYMMDD00/
    }
    else {
        my %dirm = (gfs => "GFS_025", ecmwf => "ECMWF_IFS_025", era5 => "ERA5_DATA");
        $out = sprintf("%s/%s/%s%s", $S{home}, $dirm{ $args->{getdata} }, $date, $cyc);
    }
    make_path($out) unless $S{dryrun};
    info(sprintf("%s %s %sZ f%03d-f%03d every %dh -> %s",
                 uc($args->{getdata}), $date, $cyc, $args->{start}, $end, $args->{step}, $out), 1);
    my $failed = 0;

    if ($args->{getdata} eq "gfs") {
        my $base = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl";
        my @vars = qw(HGT ICEC LAND MSLET PRES RH SNOD SOILW TMP TSOIL UGRD VGRD WEASD);
        my @plevs = qw(1000 975 950 925 900 850 800 750 700 650 600 550 500 450 400 350 300 250 200 150 100 70 50 40 30 20
                       15 10 7 5 3 2 1 0.7 0.4 0.2 0.1 0.07 0.04 0.02 0.01);
        my @other = qw(2_m_above_ground 10_m_above_ground surface mean_sea_level 0-0.1_m_below_ground
                       0.1-0.4_m_below_ground 0.4-1_m_below_ground 1-2_m_below_ground max_wind tropopause);
        my $qs = join("", map { "&var_$_=on" } @vars)
               . join("", map { "&lev_${_}_mb=on" } @plevs)
               . join("", map { "&lev_$_=on" } @other);
        my ($gw, $ge) = (POSIX::fmod($w, 360.0), POSIX::fmod($e, 360.0));   # NOMADS grid runs 0..360
        $gw += 360.0 if $gw < 0;
        $ge += 360.0 if $ge < 0;
        if ($gw > $ge) {                                # area straddles the prime meridian
            ($gw, $ge) = (0.0, 360.0);
        }
        $qs .= sprintf("&subregion=&leftlon=%s&rightlon=%s&toplat=%s&bottomlat=%s&dir=%%2Fgfs.%s%%2F%s%%2Fatmos",
                       $gw, $ge, $n, $s, $date, $cyc);
        for (my $h = $args->{start}; $h <= $end; $h += $args->{step}) {
            my $f = sprintf("gfs.t%sz.pgrb2.0p25.f%03d", $cyc, $h);
            my $o = sprintf("%s/%s%s_%s", $out, $date, $cyc, $f);
            if (-f $o && (-s $o) && !$S{force}) {
                say("[skip] $f", "", 0, 1, 6);
                next;
            }
            say("[get ] $f", "", 0, 1, 6);
            if ($S{dryrun} || fetch_binary("$base?file=$f$qs", $o)) {
                next;
            }
            $failed++;
            say("[FAIL] $f (data not yet available or request error)", "!", 0, 1, 6);
            sleep(2);
        }
    }
    elsif ($args->{getdata} eq "ecmwf") {
        my $base = "https://data.ecmwf.int/forecasts";
        for (my $h = $args->{start}; $h <= $end; $h += $args->{step}) {
            my $f = sprintf("%s%s0000-%dh-oper-fc.grib2", $date, $cyc, $h);
            my $o = sprintf("%s/%s%s_%s", $out, $date, $cyc, $f);
            if (-f $o && (-s $o) && !$S{force}) {
                say("[skip] $f", "", 0, 1, 6);
                next;
            }
            say("[get ] $f", "", 0, 1, 6);
            my $url = sprintf("%s/%s/%sz/ifs/0p25/oper/%s", $base, $date, $cyc, $f);
            if ($S{dryrun} || fetch_binary($url, $o)) {
                next;
            }
            $failed++;
            say("[FAIL] $f", "!", 0, 1, 6);
            sleep(2);
        }
    }
    else {  # era5: one request pair per calendar day covered by cycle + [start, end] hours
        my $t0 = timegm(0, 0, int($cyc), int(substr($date, 6, 2)), int(substr($date, 4, 2)) - 1,
                        int(substr($date, 0, 4)) - 1900);
        my %days;
        for (my $h = $args->{start}; $h <= $end; $h += $args->{step}) {
            my $t = $t0 + $h * 3600;
            my $d = POSIX::strftime("%Y%m%d", gmtime($t));
            push @{ $days{$d} }, sprintf("%02d:00", (gmtime($t))[2]);
        }
        if ($S{dryrun}) {
            for my $d (sort keys %days) {
                say(sprintf("would request ERA5 sfc+pl for %s (%s) via cdsapi", $d, join(",", @{ $days{$d} })),
                    "dbg", 0, 1, 6);
            }
        }
        else {
            my $py = _python_exe();
            my $have = 0;
            if ($py ne "") {
                my $rc = run(cmd => [$py, "-c", "import cdsapi"], check => 0, log => "/dev/null");
                $have = ($rc == 0) ? 1 : 0;
            }
            fatal(sprintf("The cdsapi module is missing. Run:  %s --addpack cdsapi", $EXE)) unless $have;
            my @area = ($n, $w, $s, $e);
            my $td = _mkdtemp("wrfall_cds_XXXXXX", undef);
            my $runner = "$td/cdsapi_retrieve.py";
            _spew($runner, "import cdsapi, json, sys\n" .
                           "c = cdsapi.Client()\n" .
                           "c.retrieve(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3])\n");
            for my $d (sort keys %days) {
                my $sfc = "$out/ERA5_${d}_sfc.grib";
                my $pl  = "$out/ERA5_${d}_pl.grib";
                my %req = (product_type => "reanalysis", format => "grib",
                           year => substr($d, 0, 4), month => substr($d, 4, 2), day => substr($d, 6, 2),
                           time => $days{$d}, area => \@area);
                my @jobs = (
                    [$sfc, "reanalysis-era5-single-levels", { variable => \@ERA5_SFC_VARS }],
                    [$pl,  "reanalysis-era5-pressure-levels",
                           { variable => \@ERA5_PL_VARS, pressure_level => \@ERA5_LEVELS }],
                );
                for my $job (@jobs) {
                    my ($dest, $ds, $extra) = @$job;
                    my %r = (%req, %$extra);
                    if (-f $dest && (-s $dest) && !$S{force}) {
                        say("[skip] " . basename($dest), "", 0, 1, 6);
                        next;
                    }
                    say("[get ] " . basename($dest), "", 0, 1, 6);
                    my $rc = run(cmd => [$py, $runner, $ds, JSON::PP->new->canonical(1)->encode(\%r), $dest],
                                 check => 0, inherit => 1);
                    if ($rc != 0) {
                        $failed++;
                        say(sprintf("[FAIL] %s: cdsapi request failed (exit %d)", basename($dest), $rc),
                            "!", 0, 1, 6);
                    }
                }
            }
            rm($td);
        }
    }
    info(sprintf("Done. Failed files: %d", $failed), 1);
    if ($args->{getdata} eq "era5") {
        print "      --run handles the ERA5 Vtable/prefix steps (PRES + SFC ungrib passes, fg_name) for you.\n\n";
    }
    return;
}

sub do_getdata {
    my ($args) = @_;
    my $date = $args->{date} // _utc_now_ymd();
    fatal("--date must be YYYYMMDD") if $date !~ /^\d{8}$/;
    fatal("--cycle must be 00, 06, 12 or 18")
        unless grep { $_ eq ($args->{cycle} // "") } qw(00 06 12 18);
    my $doms = resolve_domains($args->{domain});
    if (defined $args->{domain} && !@$doms) {
        fatal("No domain found for --domain " . $args->{domain});
    }
    for my $d (@$doms) {
        fatal("Domain directory does not exist: $d") unless -d $d;
    }
    my @dl = @$doms ? @$doms : ("");
    for my $d (@dl) {
        getdata_one($args, $d);
    }
    return;
}

# =============================================================================
#  --run  (run_wrf.sh)
# =============================================================================
sub archive_outputs {
    # Keep step products under <domain>/wrf_output/{geogrid,ungrib,metgrid}/.
    my ($base, $cdate, $kind) = @_;
    return if $S{dryrun};
    my $root = "$base/wrf_output/$kind";
    my $dst = ($kind eq "geogrid") ? $root : "$root/$cdate";
    make_path($dst);
    if ($kind eq "geogrid") {
        my @files = (glob("$base/geo_em.d*.nc"), "$base/log.geogrid", "$base/geogrid.log");
        for my $f (@files) {
            copy2($f, $dst) if -f $f;
        }
    }
    elsif ($kind eq "ungrib") {
        my @files = ("$base/log.ungrib", "$base/ungrib.log",
                     glob("$base/PRES:*"), glob("$base/SFC:*"), glob("$base/FILE:*"));
        for my $f (@files) {
            move($f, "$dst/" . basename($f)) if -f $f;
        }
    }
    else {
        my @files = ("$base/log.metgrid", "$base/metgrid.log", glob("$base/met_em*"));
        for my $f (@files) {
            next unless -f $f;
            if (basename($f) =~ /^met_em/) {
                move($f, "$dst/" . basename($f));
            }
            else {
                copy2($f, $dst);
            }
        }
    }
    return;
}

sub set_vtable {
    my ($base, $vtable) = @_;
    my $vt = "$base/Vtable";
    return if $S{dryrun};
    unlink($vt) if lexists_p($vt);
    symlink("ungrib/Variable_Tables/" . $vtable, $vt);   # ln -sf ungrib/Variable_Tables/<Vtable> Vtable
    return;
}

sub link_grib {
    my ($base, $env, $files) = @_;
    rm($_) for glob("$base/GRIBFILE.*");
    step(["./link_grib.csh", @$files], $base, $env, "log.link_grib");
    return;
}

sub parse_run_args {
    # --run auto [gfs|ecmwf|era5]   or   --run YYYYMMDD CC HOURS [gfs|ecmwf|era5]
    my ($r, $default_source) = @_;
    my @r = (ref($r) && @$r) ? @$r : ("auto");
    my $src = $default_source;
    if (@r && exists $SRC_DIRS{ lc($r[-1]) }) {
        $src = lc(pop @r);
    }
    if (!@r || (@r == 1 && $r[0] eq "auto")) {
        return (_utc_now_ymd(), "00", "72", $src);
    }
    if (@r == 3) {
        return ($r[0], $r[1], $r[2], $src);
    }
    fatal("Usage: --run auto [gfs|ecmwf|era5]   or   --run YYYYMMDD CYCLE FORECAST_HOURS [gfs|ecmwf|era5]");
}

sub do_run {
    my ($args) = @_;
    my $doms = resolve_domains($args->{domain});
    if (!@$doms) {
        fatal("--run needs --domain DIR[,DIR2,...] (or run it from inside a domain created with --mkdomain).");
    }
    my ($in_date, $cc, $fc, $src) = parse_run_args($args->{run}, $args->{source});
    if ($in_date !~ /^\d{8}$/ || !(grep { $_ eq $cc } qw(00 06 12 18))
        || $fc !~ /^\d+$/ || int($fc) < 1 || int($fc) > 72) {
        fatal("Bad arguments: date YYYYMMDD, cycle 00|06|12|18, hours 1-72.");
    }
    for my $d (@$doms) {
        fatal("Run directory does not exist: $d") unless -d $d;
    }
    for my $d (@$doms) {
        run_one($d, $in_date, $cc, int($fc), $src);
    }
    return;
}

sub run_one {
    my ($base, $in_date, $cc, $fc, $src) = @_;
    my $start = timegm(0, 0, int($cc), int(substr($in_date, 6, 2)), int(substr($in_date, 4, 2)) - 1,
                       int(substr($in_date, 0, 4)) - 1900);
    my $end = $start + $fc * 3600;
    my $cdate = $in_date . $cc;
    my $gribdir = date_dir($base, $src, $in_date, $cc);
    my $wrfout = "$base/wrf_output/$cdate";
    my $real_np = int($ENV{REAL_NP} // 2);
    my $wrf_np  = int($ENV{WRF_NP} // 8);

    for my $exe (qw(ungrib.exe metgrid.exe real.exe wrf.exe link_grib.csh)) {
        fatal("Required file not found in the domain: $exe") unless -e "$base/$exe";
    }
    if (!-d $gribdir && !$S{dryrun}) {
        fatal(sprintf("GRIB input directory not found: %s\n(download it with --getdata %s --domain %s --date %s --cycle %s)",
                      $gribdir, $src, $base, $in_date, $cc));
    }
    if (!$S{dryrun}) {
        make_path("$base/wrf_output/$_") for @OUT_SUBS;
    }
    my $env = build_env(undef);
    my $mpi = $S{prefix} . "/bin/mpirun";
    $mpi = (-e $mpi) ? $mpi : (which("mpirun") || "mpirun");
    my $t0 = time();
    info(sprintf("WRF run started in %s. Source=%s, date=%s, cycle=%s UTC, forecast=%d hours",
                 basename($base), $src, $in_date, $cc, $fc), 1);

    my $stamp = sub { my ($t) = @_; return POSIX::strftime("%Y-%m-%d_%H:00:00", gmtime($t)); };

    my $wpsnl = "$base/namelist.wps";
    my $wrfnl = "$base/namelist.input";
    my $nowstamp = POSIX::strftime("%Y%m%dT%H%M%S", gmtime());
    for my $f ($wpsnl, $wrfnl) {
        copy2($f, "$f.bak.$nowstamp");
    }
    for my $kv (["start_date", "'" . $stamp->($start) . "'"],
                ["end_date",   "'" . $stamp->($end) . "'"]) {
        fatal(sprintf("namelist key '%s' not found in %s", $kv->[0], $wpsnl))
            unless set_namelist_text($wpsnl, $kv->[0], $kv->[1]);
    }
    if (($ENV{RUN_GEOGRID} // "") eq "1") {
        info("Running geogrid.exe", 0);
        step(["./geogrid.exe"], $base, $env, "log.geogrid");
        archive_outputs($base, $cdate, "geogrid");
    }
    else {
        info("Skipping geogrid.exe (set RUN_GEOGRID=1 to run it)", 0);
    }

    my @files = sort grep { -f $_ } glob("$gribdir/*");
    if (!@files && !$S{dryrun}) {
        fatal("No GRIB files found in $gribdir");
    }
    for my $old (glob("$base/PRES:*"), glob("$base/SFC:*"), glob("$base/FILE:*")) {
        unlink($old) unless $S{dryrun};
    }

    info("Linking GRIB files and Vtable", 0);
    if ($src eq "era5") {
        # pressure-level and surface files are ungribbed separately (prefix PRES / SFC)
        my @pl = grep { basename($_) =~ /_pl/ } @files;
        @pl = @files unless @pl;
        my @sf = grep { basename($_) =~ /_sfc/ } @files;
        my @passes = (["PRES", \@pl, $ENV{ERA5_VTABLE_PL} // "Vtable.ERA-interim.pl"]);
        push @passes, ["SFC", \@sf, $ENV{ERA5_VTABLE_SFC} // "Vtable.ERA-interim.pl"] if @sf;
        for my $pass (@passes) {
            my ($prefix, $flist, $vt) = @$pass;
            link_grib($base, $env, $flist);
            set_vtable($base, $vt);
            set_namelist_text($wpsnl, "prefix", "'$prefix'", 1) unless $S{dryrun};
            info("Running ungrib.exe ($prefix)", 0);
            step(["./ungrib.exe"], $base, $env, "log.ungrib");
            if (!$S{dryrun} && -f "$base/log.ungrib") {
                copy2("$base/log.ungrib", "$base/log.ungrib.$prefix");
            }
        }
        if (!$S{dryrun}) {
            set_namelist_text($wpsnl, "fg_name", @sf ? "'PRES', 'SFC'" : "'PRES'", 1);
        }
    }
    else {
        link_grib($base, $env, \@files);
        set_vtable($base, $VTABLES{$src});
        if (!$S{dryrun}) {
            set_namelist_text($wpsnl, "prefix", "'FILE'", 1);
            set_namelist_text($wpsnl, "fg_name", "'FILE'", 1);
        }
        info("Running ungrib.exe", 0);
        step(["./ungrib.exe"], $base, $env, "log.ungrib");
    }
    info("Running metgrid.exe", 0);
    step(["./metgrid.exe"], $base, $env, "log.metgrid");
    if (!$S{dryrun}) {
        # real.exe needs met_em* in the run directory: copy them there, keep the originals
        # (and the ungrib intermediates) archived under wrf_output/{ungrib,metgrid}/<cycle>/
        make_path("$base/wrf_output/metgrid/$cdate");
        for my $f (glob("$base/met_em*")) {
            copy2($f, "$base/wrf_output/metgrid/$cdate/" . basename($f));
        }
        for my $f ("$base/log.metgrid") {
            if (-f $f) {
                copy2($f, "$base/wrf_output/metgrid/$cdate");
            }
        }
    }
    archive_outputs($base, $cdate, "ungrib");

    info("Updating namelist.input", 0);
    my @s = gmtime($start);
    my @e = gmtime($end);
    for my $kv (["run_hours", $fc],
                ["start_year", $s[5] + 1900], ["start_month", $s[4] + 1],
                ["start_day", $s[3]], ["start_hour", $s[2]],
                ["end_year", $e[5] + 1900], ["end_month", $e[4] + 1],
                ["end_day", $e[3]], ["end_hour", $e[2]]) {
        fatal(sprintf("namelist key '%s' not found in %s", $kv->[0], $wrfnl))
            unless set_namelist_text($wrfnl, $kv->[0], "$kv->[1]");
    }
    info(sprintf("Running real.exe with %d MPI rank(s)", $real_np), 0);
    step([$mpi, "-np", "$real_np", "./real.exe"], $base, $env, "log.real");
    info(sprintf("Running wrf.exe with %d MPI rank(s)", $wrf_np), 0);
    step([$mpi, "-np", "$wrf_np", "./wrf.exe"], $base, $env, "log.wrf");

    if (!$S{dryrun}) {
        make_path($wrfout);
        my @outs = glob("$base/wrfout_*");
        if (!@outs) {
            warnmsg("No wrfout_* files found in $base");
        }
        for my $f (@outs) {
            my $bn = basename($f);
            $bn =~ s/:/_/g;
            $bn =~ s/-/_/g;
            move($f, "$wrfout/$bn");
        }
        for my $f (glob("$base/met_em*")) {          # working copies; archived copies remain
            unlink($f);
        }
    }
    my $el = int(time() - $t0);
    okmsg(sprintf("WRF run completed. Output: %s  (elapsed %dh %dm %ds)",
                  $wrfout, int($el / 3600), int($el % 3600 / 60), $el % 60), 1);
    return;
}

sub step {
    my ($cmd, $cwd, $env, $logname) = @_;
    my $log = "$cwd/$logname";
    rm($log) unless $S{dryrun};
    run(cmd => $cmd, cwd => $cwd, env => $env, log => $log);
    return;
}

# =============================================================================
#  Main
# =============================================================================
sub _actions {
    # (present participle phrase, past phrase) for each task requested, in execution order.
    my ($args) = @_;
    my @acts;
    push @acts, ["installing WRF/WPS", "installed WRF/WPS"] if defined $args->{install};
    push @acts, ["updating WRF/WPS", "updated WRF/WPS"] if defined $args->{update};
    if (@{ $args->{addpack} } || @{ $args->{addgeog} }) {
        push @acts, ["adding optional packages/geography data", "added the optional packages/geography data"];
    }
    push @acts, ["creating the domain '$args->{mkdomain}'", "created the domain '$args->{mkdomain}'"]
        if $args->{mkdomain};
    if ($args->{getdata}) {
        my $t = uc($args->{getdata});
        push @acts, ["downloading $t data", "downloaded the $t data"];
    }
    push @acts, ["running WRF", "run WRF"] if defined $args->{run};
    return \@acts;
}

sub welcome_text {
    my ($args) = @_;
    my $acts = _actions($args);
    return "Welcome to WRFall." unless @$acts;
    return "Welcome to WRFall. You are " . join(" and ", map { $_->[0] } @$acts) . ".";
}

sub closing_text {
    my ($args) = @_;
    my $acts = _actions($args);
    return "WRFall finished. You have " . join(" and ", map { $_->[1] } @$acts) . ".";
}

sub main {
    $SIG{INT} = \&on_sigint;
    my ($args, $extra) = parse_args();
    if (!scalar(@ARGV) || $args->{help}) {
        print_help();
    }
    print_flags() if $args->{flags};
    if ($args->{version}) {
        say("What you have is the WRF installation and update tool (V$VERSION)", "", 1, 2);
        return;
    }
    if (@$extra) {
        fatal("What, exactly, do you expect to achieve by passing such gibberish (" .
              join(" ", @$extra) . ")?");
    }

    sanity_check($args);
    configure($args);

    say(welcome_text($args), "", 1, 1);

    my $did = 0;
    if (defined $args->{install}) {
        $did = 1;
        do_install($args);
    }
    if (defined $args->{update}) {
        $did = 1;
        do_update($args);
    }
    if (@{ $args->{addpack} } || @{ $args->{addgeog} }) {
        $did = 1;
        do_addpack($args);
    }
    if ($args->{mkdomain}) {
        $did = 1;
        do_mkdomain($args);
    }
    if ($args->{getdata}) {
        $did = 1;
        do_getdata($args);
    }
    if (defined $args->{run}) {
        $did = 1;
        do_run($args);
    }
    if (!$did) {
        fatal("What do you want from me?\n\n    $EXE --install -arw\n    $EXE --update\n    $EXE --addpack domwiz\n\n" .
              "Try \"$EXE --help\".");
    }
    say(closing_text($args), "", 1, 2);
    return;
}

main() unless caller;
