# Prepared from the reviewed 0.4.0 working tree. No push or tag is created.
[CmdletBinding()]
param([switch]$Execute)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$groups = @(
    @{ Message = 'fix(playback): stabilize DLNA delivery and AirPlay 2 mixer playback'; Files = @(
        'DESIGN.md'
        'docs/audio-path.md'
        'docs/local-bridge.md'
        'docs/playback-lifecycle.md'
        'micast/audio_bridge.py'
        'micast/audio_encoder.py'
        'micast/config_models.py'
        'micast/dlna.py'
        'micast/dlna_delivery.py'
        'micast/fnos_receiver.py'
        'micast/local_airplay.py'
        'micast/lyrics.py'
        'micast/main.py'
        'micast/media_cache.py'
        'micast/media_playback.py'
        'micast/media_source.py'
        'micast/pcm_source.py'
        'micast/playback_orchestrator.py'
        'micast/playback_sessions.py'
        'micast/pyav_compat.py'
        'micast/raop/server.py'
        'micast/routes/dlna.py'
        'micast/routes/playback.py'
        'micast/runtime_log.py'
        'micast/shairport_metadata.py'
        'micast/stream_server.py'
        'micast/xiaomi/device_manager.py'
        'micast/xiaomi/mina_api.py'
        'requirements.txt'
        'tests/conftest.py'
        'tests/test_airplay2_retarget.py'
        'tests/test_airplay2_variants.py'
        'tests/test_dlna.py'
        'tests/test_dlna_delivery.py'
        'tests/test_dlna_events.py'
        'tests/test_lyrics_matching.py'
        'tests/test_media_cache.py'
        'tests/test_media_playback.py'
        'tests/test_media_recovery.py'
        'tests/test_media_source.py'
        'tests/test_playback_sessions.py'
        'tests/test_runtime_log.py'
        'tests/test_startup_latency.py'
        'tests/test_startup_readiness.py'
        'tests/test_track_display.py'
        'tests/test_volume_protocols.py'
        'tests/test_xiaomi_cloud.py'
    ) }
    @{ Message = 'feat(web): refine layouts, visual effects and legal dialogs'; Files = @(
        'docs/privacy-policy.md'
        'docs/user-agreement.md'
        'scripts/sync-legal.py'
        'web/src/api.ts'
        'web/src/components/advanced-view.ts'
        'web/src/components/debug-panel.ts'
        'web/src/components/device-capabilities.ts'
        'web/src/components/devices-view.ts'
        'web/src/components/eq-curve-canvas.ts'
        'web/src/components/legal-dialog.ts'
        'web/src/components/local-bridge.ts'
        'web/src/components/player-overlay.ts'
        'web/src/components/protocol-settings.ts'
        'web/src/components/receivers-view.ts'
        'web/src/components/settings-view.ts'
        'web/src/components/support-dialog.ts'
        'web/src/components/topology-view.ts'
        'web/src/legal-documents.json'
        'web/src/main.ts'
        'web/src/styles.css'
        'web/src/styles/accessibility.css'
        'web/src/styles/components.css'
        'web/src/styles/foundation.css'
        'web/src/styles/materials.css'
        'web/src/styles/onboarding.css'
        'web/src/styles/player-experience.css'
        'web/src/styles/refinement.css'
        'web/src/styles/support.css'
        'web/src/styles/surface-corrections.css'
        'web/src/styles/tokens.css'
        'web/src/styles/topology-layout.css'
        'web/src/ui/motion.ts'
        'web/src/ui/spring.ts'
        'web/tests/frontend.test.mjs'
        'web/tsconfig.json'
    ) }
    @{ Message = 'fix(fnos): harden startup, upgrades and data retention'; Files = @(
        'docs/fnos-lifecycle-audit.md'
        'micast/installation.py'
        'packaging/fnos/README.md'
        'packaging/fnos/cmd/install_callback'
        'packaging/fnos/cmd/install_init'
        'packaging/fnos/cmd/legal-version'
        'packaging/fnos/cmd/lifecycle.py'
        'packaging/fnos/cmd/main'
        'packaging/fnos/cmd/service-process.py'
        'packaging/fnos/cmd/uninstall_callback'
        'packaging/fnos/cmd/uninstall_init'
        'packaging/fnos/cmd/upgrade_callback'
        'packaging/fnos/cmd/upgrade_init'
        'packaging/fnos/wizard/install'
        'packaging/fnos/wizard/uninstall'
        'packaging/fnos/wizard/upgrade'
        'scripts/build-fnos.sh'
        'scripts/check-fnos-package.py'
        'tests/test_fnos_lifecycle.py'
        'tests/test_fnos_packaging.py'
        'tests/test_fnos_service_process.py'
        'tests/test_fnos_startup_shell.py'
        'tests/test_installation_ports.py'
    ) }
    @{ Message = 'chore(release): prepare 0.4.0 and link MiCast Go'; Files = @(
        '.github/workflows/release-fnos.yml'
        '.github/workflows/release-windows.yml'
        'README.md'
        'docs/fnos-listing.md'
        'docs/release-notes-0.4.0.md'
        'docs/release-review-0.4.0.md'
        'micast/__init__.py'
        'packaging/fnos/manifest'
        'packaging/windows/MiCast.iss'
        'pyproject.toml'
        'scripts/commit-0.4.0.ps1'
        'scripts/update-fndepot.ps1'
    ) }
)
if (-not $Execute) {
    foreach ($group in $groups) {
        Write-Host "`n$($group.Message)"
        $group.Files | ForEach-Object { Write-Host "  $_" }
    }
    Write-Host "`nRun again with -Execute to create these four local commits."
    exit 0
}
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) { throw 'The index must be empty before running this script.' }
$known = @($groups | ForEach-Object { $_.Files })
$dirty = @(git diff --name-only)
if ($LASTEXITCODE -ne 0) { throw 'Cannot read working-tree changes.' }
$dirty += @(git ls-files --others --exclude-standard)
if ($LASTEXITCODE -ne 0) { throw 'Cannot read untracked files.' }
$unexpected = @($dirty | Where-Object { $_ -notin $known })
if ($unexpected.Count) { throw "Unclassified changes: $($unexpected -join ', ')" }
foreach ($group in $groups) {
    git add -- @($group.Files)
    if ($LASTEXITCODE -ne 0) { throw "Staging failed: $($group.Message)" }
    git diff --cached --quiet
    if ($LASTEXITCODE -eq 0) { continue }
    if ($LASTEXITCODE -ne 1) { throw 'Cannot inspect staged changes.' }
    git diff --cached --check
    if ($LASTEXITCODE -ne 0) { throw 'Staged diff validation failed.' }
    git commit -m $group.Message
    if ($LASTEXITCODE -ne 0) { throw "Commit failed: $($group.Message)" }
}
git status --short
if ($LASTEXITCODE -ne 0) { throw 'Cannot read final repository status.' }
