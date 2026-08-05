# Install the collector's scheduled tasks on the CYC PC.
#
# Everything is created DISABLED. The cutover sequence installs the task
# disabled, stops WeatherLink, proves COM4 opens, starts the collector by hand
# into shadow storage, and only then enables the schedule. Creating these
# enabled would start collecting against a port WeatherLink still owns.
#
# pythonw.exe, not python.exe: a console window owned by a SYSTEM task has
# nowhere to go, and the "cannot launch over schtasks" lesson in NOTES.md was
# about a GUI app blocking on a modal dialog. A headless interpreter has no
# such dependency.
#
# Uninstall:  .\install_tasks.ps1 -Remove

param([switch]$Remove, [switch]$Enable)

$root   = 'C:\cycwx'
$py     = "$root\python\pythonw.exe"
$app    = "$root\app"
$names  = @('CYCWX-Collect', 'CYCWX-Ship', 'CYCWX-Watchdog')

if ($Remove) {
    foreach ($n in $names) {
        schtasks /Delete /TN $n /F 2>$null | Out-Null
        Write-Output "removed $n"
    }
    Write-Output "C:\cycwx is left in place: it is the rollback artifact."
    exit 0
}

if ($Enable) {
    foreach ($n in $names) {
        schtasks /Change /TN $n /ENABLE | Out-Null
        Write-Output "enabled $n"
    }
    exit 0
}

# --- collector: starts at boot, runs forever, restarted by the watchdog ------
# ExecutionTimeLimit is unlimited because it is a long-running reader. That is
# exactly why the watchdog exists: with no time limit and IgnoreNew, a wedged
# instance would hold COM4 for ever and the scheduler would never replace it.
schtasks /Create /TN 'CYCWX-Collect' /F /RU SYSTEM /RL HIGHEST /SC ONSTART `
    /TR "`"$py`" `"$app\collect.py`" --port COM4 --spool $root\send --heartbeat $root\heartbeat.json --log $root\log\collect.log --quiet" | Out-Null
schtasks /Change /TN 'CYCWX-Collect' /RI 0 2>$null | Out-Null
Write-Output 'created CYCWX-Collect (disabled)'

# --- shipper: every minute, matching the cadence the CSV has always had ------
schtasks /Create /TN 'CYCWX-Ship' /F /RU SYSTEM /RL HIGHEST /SC MINUTE /MO 1 `
    /TR "`"$py`" `"$app\ship.py`" --spool $root\send --keyfile $root\etc\cyc_ingest.key --log $root\log\ship.log --quiet" | Out-Null
Write-Output 'created CYCWX-Ship (disabled)'

# --- watchdog: every two minutes, half the staleness limit ------------------
schtasks /Create /TN 'CYCWX-Watchdog' /F /RU SYSTEM /RL HIGHEST /SC MINUTE /MO 2 `
    /TR "`"$py`" `"$app\watchdog.py`" --heartbeat $root\heartbeat.json --task CYCWX-Collect" | Out-Null
Write-Output 'created CYCWX-Watchdog (disabled)'

foreach ($n in $names) {
    schtasks /Change /TN $n /DISABLE | Out-Null
}
Write-Output ''
Write-Output 'All three tasks are DISABLED. Enable them only at cutover step 7,'
Write-Output 'after the collector has been proven by hand against a released COM4:'
Write-Output '    .\install_tasks.ps1 -Enable'
