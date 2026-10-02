param(
    [ValidateSet('catalogs', 'registry')][string]$Mode = 'catalogs',
    [string[]]$Repositories = @()
)

$ErrorActionPreference = 'Stop'
$headers = @{ 'User-Agent' = 'Agent-Forge-Source-Inventory' }
$outputDirectory = $PSScriptRoot
$publicSession = [Microsoft.PowerShell.Commands.WebRequestSession]::new()

function Read-PublicJson([string]$Url) {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            if ($Url.StartsWith('https://api.github.com/')) {
                $response = & gh api $Url.Substring('https://api.github.com/'.Length)
                if ($LASTEXITCODE -ne 0) { throw "GitHub CLI request failed: $Url" }
                return ($response -join "`n" | ConvertFrom-Json)
            }
            return Invoke-RestMethod -Uri $Url -Headers $headers -WebSession $publicSession -TimeoutSec 45
        } catch {
            if ($attempt -eq 3) { throw }
            Start-Sleep -Seconds 2
        }
    }
}

function Save-Evidence([string]$Name, $Value) {
    $Value | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath (Join-Path $outputDirectory $Name) -Encoding utf8
}

function Read-RepositoryText([string]$Repository, [string]$Path, [string]$Revision) {
    $file = Read-PublicJson "https://api.github.com/repos/$Repository/contents/${Path}?ref=$Revision"
    if (-not $file.content) {
        $file = Read-PublicJson "https://api.github.com/repos/$Repository/git/blobs/$($file.sha)"
    }
    if (-not $file.content) { throw "No file content returned: $Repository/$Path" }
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($file.content))
}

if ($Mode -eq 'registry') {
    $result = [ordered]@{
        endpoint = 'https://registry.modelcontextprotocol.io/v0.1/servers?limit=100&version=latest'
        startedAt = [DateTime]::UtcNow.ToString('o')
        completed = $false
        pages = @()
        records = 0
        identities = @()
        uniqueNameComparison = 'ordinal-case-sensitive'
        error = $null
    }
    $identities = [Collections.Generic.List[object]]::new()
    $pages = [Collections.Generic.List[object]]::new()
    $cursors = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $url = $result.endpoint
    try {
        for ($page = 1; $page -le 1000; $page++) {
            $response = Read-PublicJson $url
            if (-not $response.PSObject.Properties['servers']) { throw 'Missing servers array' }
            foreach ($entry in $response.servers) {
                $identities.Add([ordered]@{
                    name = $entry.server.name
                    version = $entry.server.version
                    status = $entry._meta.'io.modelcontextprotocol.registry/official'.status
                    isLatest = $entry._meta.'io.modelcontextprotocol.registry/official'.isLatest
                })
            }
            $pages.Add([ordered]@{ page = $page; count = @($response.servers).Count; nextCursor = $response.metadata.nextCursor })
            if ($page % 25 -eq 0) {
                $result.pages = @($pages.ToArray())
                $result.records = $identities.Count
                $result.identities = @($identities.ToArray())
                Save-Evidence 'mcp-registry.json' $result
                Write-Output "MCP pages=$page records=$($identities.Count)"
            }
            $cursor = $response.metadata.nextCursor
            if (-not $cursor) { $result.completed = $true; break }
            if (-not $cursors.Add($cursor)) { throw 'Repeated pagination cursor' }
            $url = $result.endpoint + '&cursor=' + [Uri]::EscapeDataString($cursor)
        }
    } catch { $result.error = $_.Exception.Message }
    $result.pages = @($pages.ToArray())
    $result.records = $identities.Count
    $result.identities = @($identities.ToArray())
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($identity in $identities) { [void]$names.Add($identity['name']) }
    $result.uniqueNames = $names.Count
    $result.latestFlags = @($identities | ForEach-Object { [string]$_['isLatest'] } | Group-Object | Select-Object Name, Count)
    $result.statusCounts = @($identities | ForEach-Object { [string]$_['status'] } | Group-Object | Select-Object Name, Count)
    $result.finishedAt = [DateTime]::UtcNow.ToString('o')
    Save-Evidence 'mcp-registry.json' $result
    [ordered]@{
        completed = $result.completed
        records = $result.records
        uniqueNames = $result.uniqueNames
        startedAt = $result.startedAt
        finishedAt = $result.finishedAt
        error = $result.error
    } | ConvertTo-Json
    exit
}

if (-not $Repositories.Count) {
    $discovery = Read-PublicJson 'https://api.github.com/search/repositories?q=awesome-dsh&per_page=100'
    Save-Evidence 'discovery.json' ([ordered]@{
        queriedAt = [DateTime]::UtcNow.ToString('o')
        query = 'awesome-dsh'
        total = $discovery.total_count
        incomplete = $discovery.incomplete_results
        repositories = @($discovery.items | Select-Object full_name, html_url, description, default_branch, fork)
    })
}

# Count file entries and explicit collection fields; never use a JSON object's wrapper as a record count.
$specifications = @(
    @{ repo = 'awesome-dsh-plugin/awesome-dsh-plugin'; patterns = @('^data/plugins/[^/]+\.ya?ml$') },
    @{ repo = 'beancookie/awesome-dsh-plugin'; datasets = @(@{ path = 'docs/plugins.json'; field = 'plugins' }) },
    @{ repo = 'bruc3van/awesome-dsh-plugin'; datasets = @(@{ path = 'data/repositories.json'; field = 'repositories' }, @{ path = 'data/packages.json'; field = 'entries' }) },
    @{ repo = 'dshworks/awesome-dsh-plugins'; datasets = @(@{ path = 'data/plugins.json'; field = 'plugins' }, @{ path = 'data/candidates.json'; field = 'candidates' }) },
    @{ repo = 'cccakeee/awesome-dsh-plugins'; csv = @('data/repositories.csv', 'data/verified-plugins.csv') },
    @{ repo = 'kejixiaoliang/awesome-dsh-plugins'; inspect = $true; markdown = @('INDEX.md') },
    @{ repo = 'awesome-deepseekharness/awesome-deepseek-harness'; markdown = @('README.md') },
    @{ repo = 'white0dew/awesome-dsh-plugins'; datasets = @(@{ path = 'data/sources/github-plugin-catalog.json'; field = 'plugins' }, @{ path = 'data/sources/github-topic-dsh-plugin.json'; field = 'records' }); inspect = $true },
    @{ repo = 'wgd753/awesome-dsh-plugin'; datasets = @(@{ path = 'data/repositories.json' }) },
    @{ repo = 'fjzzwxp/awesome-dsh-plugins'; patterns = @('^data/plugins/[^/]+\.ya?ml$') },
    @{ repo = 'hackerFish/awesome-dsh-skills'; patterns = @('(^|/)SKILL\.md$') },
    @{ repo = 'yzfly/awesome-dsh-skills'; datasets = @(@{ path = 'data/skills.json' }, @{ path = 'data/candidates.json' }); patterns = @('(^|/)SKILL\.md$') },
    @{ repo = 'zhiwehu/awesome_dsh_skills'; patterns = @('(^|/)SKILL\.md$') },
    @{ repo = 'hackerFish/awesome-dsh-presets'; patterns = @('^presets/[^/]+/preset\.yml$') },
    @{ repo = 'dataelement/awesome-dsh-workbench'; patterns = @('^data/workbenches/[^/]+\.ya?ml$') },
    @{ repo = 'dshworks/awesome-dsh-themes'; datasets = @(@{ path = 'data/themes.json'; field = 'themes' }, @{ path = 'data/candidates.json'; field = 'candidates' }) },
    @{ repo = 'Renakoni/awesome-dsh-themes'; patterns = @('^entries/[^/]+/theme\.yml$'); datasets = @(@{ path = 'data/catalog.json'; field = 'themes' }) },
    @{ repo = 'web-casa/awesome-cordis-plugins'; datasets = @(@{ path = 'data/plugins.json'; field = 'plugins' }) },
    @{ repo = 'kingselyjoe/awesome-dsh-list'; markdown = @('README.md'); inspect = $true },
    @{ repo = 'the-beating-light-of-the-nail/awesome-dsh-plugin-stock'; markdown = @('README.md') },
    @{ repo = 'YYTbit/awesome-dsh-bridges'; markdown = @('README.md'); packageNames = $true },
    @{ repo = 'anthropics/claude-plugins-official'; datasets = @(@{ path = '.claude-plugin/marketplace.json'; field = 'plugins' }); patterns = @('(^|/)SKILL\.md$') },
    @{ repo = 'anthropics/skills'; patterns = @('^skills/[^/]+/SKILL\.md$') },
    @{ repo = 'ComposioHQ/awesome-claude-skills'; patterns = @('(^|/)SKILL\.md$') },
    @{ repo = 'obra/superpowers'; patterns = @('^skills/[^/]+/SKILL\.md$') },
    @{ repo = 'addyosmani/agent-skills'; patterns = @('(^|/)SKILL\.md$') },
    @{ repo = 'VoltAgent/awesome-agent-skills'; markdown = @('README.md') },
    @{ repo = 'modelcontextprotocol/servers'; patterns = @('^src/[^/]+/README\.md$'); markdown = @('README.md') },
    @{ repo = 'punkpeye/awesome-mcp-servers'; markdown = @('README.md') },
    @{ repo = 'e2b-dev/awesome-ai-agents'; markdown = @('README.md') }
)
$results = [Collections.Generic.List[object]]::new()
if ($Repositories.Count) {
    $specifications = @($specifications | Where-Object { $Repositories -contains $_.repo })
    $existing = Get-Content -LiteralPath (Join-Path $outputDirectory 'catalogs.json') -Raw | ConvertFrom-Json -AsHashtable
    foreach ($record in $existing) {
        if ($Repositories -notcontains $record.repository) { $results.Add($record) }
    }
}
foreach ($specification in $specifications) {
    $repo = $specification.repo
    $record = [ordered]@{ repository = $repo; url = "https://github.com/$repo"; observedAt = [DateTime]::UtcNow.ToString('o'); measurements = @(); errors = @() }
    try {
        $tree = Read-PublicJson "https://api.github.com/repos/$repo/git/trees/HEAD?recursive=1"
        $record.revision = $tree.sha
        $record.truncated = $tree.truncated
        if ($tree.truncated) { throw 'Tree truncated; file counts would be incomplete' }
        foreach ($pattern in $specification.patterns) {
            $paths = @($tree.tree | Where-Object { $_.type -eq 'blob' -and $_.path -cmatch $pattern } | Select-Object -ExpandProperty path)
            $record.measurements += @{ method = 'tree-files'; pattern = $pattern; count = $paths.Count; entries = $paths }
        }
        if ($specification.inspect) {
            $record.dataPaths = @($tree.tree | Where-Object { $_.type -eq 'blob' -and $_.path -match '(\.(json|csv|ya?ml)$|INDEX\.md)' -and $_.path -notmatch '(lock|node_modules|workflow|tsconfig)' } | Select-Object -ExpandProperty path)
        }
        foreach ($dataset in $specification.datasets) {
            try {
                $content = Read-RepositoryText $repo $dataset.path $tree.sha
                $value = ConvertFrom-Json -InputObject $content -AsHashtable -NoEnumerate
                $measurement = [ordered]@{ method = 'json'; path = $dataset.path }
                if ($dataset.field) { $value = $value[$dataset.field]; $measurement.field = $dataset.field }
                if ($null -eq $value) { throw 'Missing collection field' }
                if ($value -is [Collections.IDictionary]) {
                    if (@($value.Keys | Where-Object { $_ -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' }).Count) {
                        throw 'Object is not a repository-keyed collection; specify a collection field'
                    }
                    $measurement.shape = 'object'
                    $measurement.keys = @($value.Keys)
                    $measurement.count = $value.Count
                    $measurement.sample = @($value.Values | Select-Object -First 1)
                } elseif ($value -is [array]) {
                    $measurement.shape = 'array'
                    $measurement.count = $value.Count
                    $measurement.sample = @($value | Select-Object -First 1)
                    $measurement.entries = @($value | ForEach-Object {
                        if ($_ -is [Collections.IDictionary]) {
                            $identity = [ordered]@{}
                            foreach ($key in @('id', 'name', 'repo', 'repository', 'url', 'full_name', 'type', 'kind', 'category', 'source')) {
                                if ($_.Contains($key)) { $identity[$key] = $_[$key] }
                            }
                            $identity
                        } else { $_ }
                    })
                } else { throw 'Unexpected scalar collection' }
                $record.measurements += $measurement
            } catch { $record.errors += "$($dataset.path): $($_.Exception.Message)" }
        }
        foreach ($path in $specification.csv) {
            try {
                $rows = @(Read-RepositoryText $repo $path $tree.sha | ConvertFrom-Csv)
                $record.measurements += @{ method = 'csv'; path = $path; count = $rows.Count; entries = @($rows | Select-Object repo, repository, url, name, full_name) }
            } catch { $record.errors += "${path}: $($_.Exception.Message)" }
        }
        foreach ($path in $specification.markdown) {
            try {
                $content = Read-RepositoryText $repo $path $tree.sha
                $lines = @($content -split '\r?\n' | Where-Object { $_ -match '^\s*(\||[-*]\s+|\d+\.\s+)' -and $_ -match '\]\(https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+' })
                $links = @([regex]::Matches(($lines -join "`n"), '\]\(https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)') | ForEach-Object { $_.Groups[1].Value.TrimEnd('.') } | Sort-Object -Unique)
                $record.measurements += @{ method = 'markdown-repository-links'; path = $path; count = $links.Count; entries = $links; lines = $lines; warning = 'Repository-link count is a discovery proxy, not a package count.' }
                if ($specification.packageNames) {
                    $names = @([regex]::Matches($content, '(?m)^- (dsh-plugin-[A-Za-z0-9_-]+) -- ') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
                    $record.measurements += @{ method = 'markdown-package-names'; path = $path; count = $names.Count; entries = $names }
                }
                $skillLinks = @([regex]::Matches($content, '\]\((https://github\.com/[^)\s]+/SKILL\.md)\)') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
                if ($skillLinks.Count) { $record.measurements += @{ method = 'markdown-skill-file-links'; path = $path; count = $skillLinks.Count; entries = $skillLinks } }
                $projectHeadings = @($content -split '\r?\n' | Where-Object { $_ -match '^## \[[^\]]+\]\(https?://' })
                if ($projectHeadings.Count) { $record.measurements += @{ method = 'markdown-project-headings'; path = $path; count = $projectHeadings.Count; entries = $projectHeadings } }
            } catch { $record.errors += "${path}: $($_.Exception.Message)" }
        }
    } catch { $record.errors += $_.Exception.Message }
    $results.Add($record)
    Save-Evidence 'catalogs.json' @($results.ToArray())
    Write-Output "$repo $(@($record.measurements | ForEach-Object { "$($_.method):$($_.count)" }) -join ', ') errors=$($record.errors.Count)"
}

if ($Repositories.Count) { exit }

$queries = @(
    'https://api.github.com/search/repositories?q=topic:dsh-plugin&per_page=1',
    'https://api.github.com/search/repositories?q=topic:dsh-skill&per_page=1',
    'https://api.github.com/search/repositories?q=topic:deepseek-harness&per_page=1',
    'https://registry.npmjs.org/-/v1/search?text=keywords:dsh-plugin&size=1',
    'https://registry.npmjs.org/-/v1/search?text=keywords:mcp-server&size=1'
)
$searches = foreach ($url in $queries) {
    try {
        $response = Read-PublicJson $url
        [ordered]@{ url = $url; observedAt = [DateTime]::UtcNow.ToString('o'); total = $response.total; total_count = $response.total_count; incomplete_results = $response.incomplete_results; method = 'api-reported-total'; sample = @($response.items | Select-Object -First 1 full_name, html_url); npmSample = @($response.objects | Select-Object -First 1) }
    } catch { [ordered]@{ url = $url; error = $_.Exception.Message } }
}
Save-Evidence 'search-totals.json' @($searches)
