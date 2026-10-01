param(
    [string]$Tag = 'latest'
)

$ErrorActionPreference = 'Stop'
$repo = 'DyMode/MiCast'
$endpoint = if ($Tag -eq 'latest') { 'latest' } else { "tags/$Tag" }
$release = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/$endpoint"
if ($release.draft -or $release.prerelease) { throw '只收录正式公开版本。' }
$version = $release.tag_name -replace '^v', ''
$base = "https://raw.githubusercontent.com/$repo/$($release.tag_name)"
$manifest = [string](Invoke-RestMethod "$base/packaging/fnos/manifest")
if ($manifest -notmatch '(?m)^appname=micast\r?$' -or
    $manifest -notmatch "(?m)^version=$([regex]::Escape($version))\r?`$") {
    throw '发布版本与 manifest 不一致。'
}
$minimum = [regex]::Match($manifest, '(?m)^os_min_version=([^\r\n]+)').Groups[1].Value
$privilege = Invoke-RestMethod "$base/packaging/fnos/config/privilege"
$packages = [ordered]@{}
foreach ($arch in @('x86', 'arm')) {
    $asset = @($release.assets | Where-Object name -eq "micast-$arch-$version.fpk")
    if ($asset.Count -ne 1) { throw "缺少唯一的 $arch 安装包。" }
    if ($asset[0].digest -notmatch '^sha256:([a-f0-9]{64})$') { throw "缺少 $arch SHA256。" }
    $packages[$arch] = [ordered]@{
        download_url = $asset[0].browser_download_url
        sha256 = $Matches[1]
        size = $asset[0].size
    }
}
$path = Join-Path $PSScriptRoot '../fnpack.json'
$source = if (Test-Path $path) {
    Get-Content $path -Raw | ConvertFrom-Json -AsHashtable
} else {
    [ordered]@{
        schema_version = '2'
        source_info = [ordered]@{
            name = 'MiCast 应用源'
            author = 'DyMode'
            homepage = "https://github.com/$repo"
            description = 'MiCast 开发者维护的飞牛原生应用源。'
        }
        apps = [ordered]@{
            micast = [ordered]@{
                display_name = 'MiCast'
                desc = '通过 AirPlay / DLNA 将手机、电脑音频投放到小米智能音箱，支持跨型号多音箱同步、立体声组合和逐台 EQ。依赖飞牛应用中心 Python 3.12；实验性 AirPlay 2 仅 x86 包提供。'
                platform = @('x86', 'arm')
                categories = @('影音娱乐', '智能智控')
                maintainer = 'Dy'
                maintainer_url = 'https://github.com/DyMode'
                bug_report_url = "https://github.com/$repo/issues"
                run_as = 'root'
                install_type = ''
                is_docker = $false
                releases = [ordered]@{}
            }
        }
    }
}
$app = $source.apps.micast
$app.icon_url = "$base/assets/icons/web/icon-256.png"
$app.preview_urls = @( '01-player', '02-speakers', '03-eq', '04-links', '05-stereo' | ForEach-Object { "$base/docs/screenshots/$_.png" })
$app.readme_url = "$base/README.md"
$app.releases[$version] = [ordered]@{
    changelog = $release.body
    updated_at = ([datetimeoffset]$release.published_at).ToString('o')
    os_min_version = $minimum
    run_as = $privilege.defaults.'run-as'
    packages = $packages
}
$source | ConvertTo-Json -Depth 20 | Set-Content $path -Encoding utf8NoBOM
Write-Output "已更新 FnDepot 源：$version（x86、arm）"
