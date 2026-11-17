param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$repoRoot = Split-Path $PSScriptRoot -Parent
$cases = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'fixtures/backend_cases.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json
[IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
$synthesizer = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $available = @($synthesizer.GetInstalledVoices() | Where-Object Enabled)
    foreach ($case in $cases.cases) {
        $voice = $available | Where-Object { $_.VoiceInfo.Culture.Name -eq $case.culture } | Select-Object -First 1
        if (-not $voice) { Write-Output "SKIP $($case.id): no installed $($case.culture) system voice"; continue }
        $synthesizer.SelectVoice($voice.VoiceInfo.Name)
        $path = Join-Path $OutputDirectory ($case.id + '.wav')
        $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
        $synthesizer.SetOutputToWaveFile($path, $format)
        $synthesizer.Speak($case.source)
        $synthesizer.SetOutputToNull()
        Write-Output "READY $($case.id): $($voice.VoiceInfo.Name)"
    }
} finally { $synthesizer.Dispose() }
