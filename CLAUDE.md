# Lippy

Local dictation for macOS. Hold a key, speak, release, and cleaned text is
pasted at the cursor. Speech recognition and the optional cleanup pass both run
on the user's machine.

## Shape

Two processes, split for one reason: loading the models takes about 25 seconds,
and nobody waits that long to dictate a sentence.

- `app/` is a Swift menu-bar app. It owns the hotkey, the microphone, the HUD
  and the paste. It is native because macOS binds Accessibility and Microphone
  permissions to a code signature, and a signed `.app` keeps those across
  rebuilds where a Python process does not.
- `daemon/` is a Python service holding the models resident, reachable only over
  a `0600` unix socket. No TCP port is opened.

**On any platform without that permission constraint, this should be one
process.** The split is a macOS workaround, not an architecture worth copying.

## The part that matters most

`daemon/rules.py` and the four guards in `daemon/polish.py` are the reason
pasted text can be trusted without proofreading. Cleanup is a dial: `raw`,
`fillers`, `clean`, `polish`. Only the last uses a language model. Everything
below it is deterministic regex and runs in about a millisecond.

The LLM's output is treated as a proposal, never a result. Four guards (length
ratio, content-word retention, interrogative preservation, meta-commentary)
reject it and fall back to the rule-cleaned text. **Do not weaken these, and do
not duplicate them per platform.** If a platform needs different behaviour it
needs a parameter, not a copy.

## Working here

```
make test          # 104 tests, offline, fast
make app           # build and sign the bundle
make install       # replace the copy in ~/Applications
make dmg           # distributable disk image
```

The Python environment lives at `~/.cache/lippy-venv`. Invoke it by path
(`~/.cache/lippy-venv/bin/python`) rather than relying on PATH.

`daemon/lippyctl.py file <wav>` runs any audio through the full pipeline and
prints the raw transcript and the final text separately, plus which guard fired
if the polish pass fell back. That separation is usually enough to tell whether
a bad result came from mishearing or from over-editing.

## Conventions

- **Signing does not guess.** With more than one candidate identity the build
  refuses and asks for `SIGN_IDENTITY`, because picking the first match once
  signed a release with the wrong certificate. A gitignored `.signing-identity`
  file records a maintainer's choice.
- **Failures here are silent by nature.** An audio converter returning zero
  frames without erroring, an entitlement refused before the permission system
  is consulted, an exception swallowed by the UI framework: none of these print
  anything. The app writes its own log to
  `~/Library/Application Support/Lippy/app.log`, because the system log needs
  admin rights. Prefer a build that fails loudly over one that ships quietly
  broken.
- **Prose style**: no em-dashes or semicolons, spell out "and", no decorative
  emoji, and no other product named. Privacy is stated as the reader's own
  position ("the audio stays on your machine"), never as a promise from a
  vendor.
- **Plans live in `docs/plans/`.** They describe work that does not exist yet.
  A plan describing something that already ships has outlived its purpose.

## Not built, on purpose

`docs/plans/` covers what is planned. The README's *Declined* section covers
what was considered and rejected, with reasons. Read it before proposing a
feature that looks obviously missing, because two of them were built and removed
after use.
