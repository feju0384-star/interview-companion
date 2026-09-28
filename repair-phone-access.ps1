# Allow only this running app's TCP port from devices on its local network.
# Run elevated. Does not change firewall profiles or disable the firewall.
param([switch]$RequestElevation)
$ErrorActionPreference = 'Stop'
$taskResultPath = Join-Path $PSScriptRoot '.local\firewall-result.json'
$taskRuleName = 'InterviewCompanion-LAN-8765'
[IO.Directory]::CreateDirectory((Split-Path -Parent $taskResultPath)) | Out-Null
try {
    $taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $taskPrincipal = New-Object Security.Principal.WindowsPrincipal($taskIdentity)
    if (-not $taskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        if ($RequestElevation) {
            $taskArguments = '-NoProfile -ExecutionPolicy RemoteSigned -File "' + $PSCommandPath + '"'
            $taskElevated = Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -ArgumentList $taskArguments -Wait -PassThru
            exit $taskElevated.ExitCode
        }
        throw 'Administrator approval is required to add the app firewall rule.'
    }
    $taskListener = @(Get-NetTCPConnection -State Listen -LocalPort 8765)
    if ($taskListener.Count -ne 1) { throw 'Expected one running app listener on port 8765.' }
    $taskProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskListener[0].OwningProcess)
    $taskScript = Join-Path $PSScriptRoot 'run.py'
    if (-not $taskProcess.CommandLine.Contains($taskScript)) {
        throw 'The listening process does not belong to this app.'
    }
    $taskRunningProgram = $taskProcess.ExecutablePath
    if ([IO.Path]::GetFileName($taskRunningProgram) -notin @('pythonw.exe', 'python.exe')) {
        throw 'Unexpected app executable.'
    }
    $taskNetwork = @(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' })
    if ($taskNetwork.Count -ne 1) { throw 'Multiple active networks: select the phone network before adding a rule.' }
    $taskInterface = $taskNetwork[0].InterfaceAlias
    $taskVerified = @()
    # Python's venv launchers can resolve to the base interpreter. Use the actual
    # listener's executable directory and support both normal and windowless runs.
    foreach ($taskExecutable in @('pythonw.exe', 'python.exe')) {
        $taskProgram = Join-Path (Split-Path -Parent $taskRunningProgram) $taskExecutable
        if (-not (Test-Path -LiteralPath $taskProgram -PathType Leaf)) { continue }
        $taskCurrentRule = if ($taskExecutable -eq 'python.exe') { $taskRuleName + '-Console' } else { $taskRuleName }
        $taskExisting = Get-NetFirewallRule -Name $taskCurrentRule -ErrorAction SilentlyContinue
        if ($taskExisting) {
            if ($taskExisting.Group -ne 'InterviewCompanion') { throw 'An unrelated rule uses this name.' }
            Set-NetFirewallRule -Name $taskCurrentRule -Enabled True -Direction Inbound -Action Allow -Profile Any -Program $taskProgram -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet -InterfaceAlias $taskInterface -EdgeTraversalPolicy Block | Out-Null
        } else {
            New-NetFirewallRule -Name $taskCurrentRule -DisplayName ('InterviewCompanion phone access (' + $taskExecutable + ', local network only)') -Group 'InterviewCompanion' -Enabled True -Direction Inbound -Action Allow -Profile Any -Program $taskProgram -Protocol TCP -LocalPort 8765 -RemoteAddress LocalSubnet -InterfaceAlias $taskInterface -EdgeTraversalPolicy Block | Out-Null
        }
        $taskRule = Get-NetFirewallRule -PolicyStore ActiveStore -Name $taskCurrentRule
        $taskPort = $taskRule | Get-NetFirewallPortFilter
        $taskAddress = $taskRule | Get-NetFirewallAddressFilter
        $taskApplication = $taskRule | Get-NetFirewallApplicationFilter
        $taskInterfaceFilter = $taskRule | Get-NetFirewallInterfaceFilter
        if ($taskRule.Enabled -ne 'True' -or $taskRule.Action -ne 'Allow' -or $taskPort.Protocol -ne 'TCP' -or $taskPort.LocalPort -ne '8765' -or $taskAddress.RemoteAddress -ne 'LocalSubnet' -or $taskApplication.Program -ne $taskProgram -or $taskInterfaceFilter.InterfaceAlias -ne $taskInterface) {
            throw 'The effective firewall rule did not match the requested scope.'
        }
        $taskVerified += @{rule=$taskCurrentRule; program=$taskProgram}
    }
    if ($taskVerified.Count -eq 0) { throw 'No valid Python executable was found.' }
    @{ok=$true; rules=$taskVerified; running_program=$taskRunningProgram; interface_alias=$taskInterface; remote_address='LocalSubnet'; port=8765; checked_at=(Get-Date).ToString('o')} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $taskResultPath -Encoding UTF8
} catch {
    @{ok=$false; error=$_.Exception.Message; checked_at=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $taskResultPath -Encoding UTF8
    exit 1
}
