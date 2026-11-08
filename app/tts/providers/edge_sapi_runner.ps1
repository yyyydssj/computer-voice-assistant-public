# Offline Edge-slot SAPI renderer for the P7 dual-path streaming plan.
#
# This script is intentionally pure ASCII: Windows PowerShell 5.1 reads a .ps1
# file without a BOM as ANSI, so any non-ASCII byte here would corrupt the
# parser. Sentence text is therefore never embedded in this file and never
# passed on the command line; it is read from -TextPath as UTF-8.
#
# Contract:
#   * -ListVoices prints one "Name|Culture|Enabled" line per installed voice
#     and nothing else, then exits 0.
#   * otherwise the UTF-8 text in -TextPath is spoken by -VoiceName and written
#     to -OutPath as 24000 Hz / 16-bit / mono PCM WAV, then exits 0.
#   * every failure exits 1 without ever printing the sentence text.

[CmdletBinding()]
param(
    [string]$TextPath,
    [string]$OutPath,
    [string]$VoiceName,
    [switch]$ListVoices
)

$ErrorActionPreference = 'Stop'

try {
    Add-Type -AssemblyName System.Speech
} catch {
    exit 1
}

if ($ListVoices) {
    $synth = $null
    try {
        $synth = New-Object -TypeName System.Speech.Synthesis.SpeechSynthesizer
    } catch {
        exit 1
    }
    try {
        foreach ($voice in $synth.GetInstalledVoices()) {
            $info = $voice.VoiceInfo
            Write-Output ("{0}|{1}|{2}" -f $info.Name, $info.Culture.Name, $voice.Enabled)
        }
    } catch {
        exit 1
    } finally {
        if ($synth -ne $null) { $synth.Dispose() }
    }
    exit 0
}

if ([string]::IsNullOrEmpty($TextPath) -or [string]::IsNullOrEmpty($OutPath) -or [string]::IsNullOrEmpty($VoiceName)) {
    exit 1
}

$text = $null
try {
    $text = [System.IO.File]::ReadAllText($TextPath, [System.Text.Encoding]::UTF8)
} catch {
    exit 1
}

if ([string]::IsNullOrWhiteSpace($text)) {
    exit 1
}

$synth = $null
try {
    $synth = New-Object -TypeName System.Speech.Synthesis.SpeechSynthesizer
} catch {
    exit 1
}

try {
    $synth.SelectVoice($VoiceName)
    $format = New-Object -TypeName System.Speech.AudioFormat.SpeechAudioFormatInfo -ArgumentList @(
        24000,
        ([System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen),
        ([System.Speech.AudioFormat.AudioChannel]::Mono)
    )
    $synth.SetOutputToWaveFile($OutPath, $format)
    $synth.Speak($text)
    $synth.SetOutputToNull()
} catch {
    exit 1
} finally {
    if ($synth -ne $null) { $synth.Dispose() }
}

exit 0
