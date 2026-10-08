# Voices

Every sub-folder here is a voice, named after the folder (`--voice <name>`).
Folders starting with `_` are ignored (`_template/` shows every option).

## Chatterbox clone of a friend's voice

1. Make `voice-refs/<name>/` and drop in one clean clip: ~10 s or more, one speaker,
   no music (wav, mp3, flac, ogg or m4a; stereo and 44.1 kHz are fine).
2. Optional `voice.yaml` next to it (copy `_template/voice.yaml`):
   ```yaml
   description: Sam's voice
   consent: {by: Sam Example, date: 2026-10-07, scope: sb01 demos at CSUSB}
   engine: nano          # English with Nano (fast); zh / ja / es clone the same clip with Multilingual
   ```
3. Check it: `python3 non-robot-testmode/run_local.py --list-voices`, then
   `python3 non-robot-testmode/run_local.py --voice <name> --say "Hello there!"`.

Without `voice.yaml` the clip is cloned with `tts.clone_defaults` (Multilingual for every language).
The runner prints a note while a cloned voice has no `consent:` recorded. Get the person's OK first.

Audio here is gitignored (`experimental/.gitignore`). Note `testmp/1007.MP3` was committed before
that rule existed, so git still tracks it; `git rm --cached` it if it should leave the repo.

## UTAU voicebank (Kasane Teto)

The `teto` voice in `config.yaml` points at banks installed outside the repo, because the
Teto terms allow free non-commercial use but **prohibit redistribution**:

| Language | Bank | Folder with `oto.ini` |
|---|---|---|
| en | TETO-English-150401 (CVVC, X-SAMPA aliases) | `~/voicebanks/teto/english/重音テト音声ライブラリー/重音テト英語音源` |
| ja | TETO-tandoku-100619 (CV) | `~/voicebanks/teto/japanese-cv/重音テト音声ライブラリー/重音テト単独音` |

Download both from https://kasaneteto.jp/utau/ and unzip them there (or set other paths in
`config.local.yaml`). Read `使用許諾条件.txt` in each zip; commercial use needs approval.
Never copy a bank into this repo.

Another bank can be a folder voice: put `bank: /path/to/folder-with-oto.ini` in
`voice-refs/<name>/voice.yaml` (English banks must use CVVC X-SAMPA aliases like Teto English;
Japanese banks kana CV aliases).

The engine's WORLD analysis of each sample is cached in `~/.cache/sb01-utau` (derived data,
safe to delete; it is rebuilt in the background on the next start: about 50 s and 60 MB for
both banks on the Alienware PC).
