# Lippy on Windows, Part 2 of 2: packaging and distribution

*Part 2 of 2. Depends on Part 1 having landed: a Windows client that runs from
source. Written 2026-08-25, before any Windows code.*

## Goal

Turn the working Windows client into something a stranger can install. A signed
executable, an MSIX package for the Microsoft Store, and a release workflow that
produces both from a version tag, the way the macOS release already works.

Split from Part 1 deliberately. The client and its packaging fail for unrelated
reasons and are verified differently: one by running it, the other by CI
artifacts and store review. Bundling them means a packaging problem blocks a
client that already works.

## Read first

- `.github/workflows/release.yml`: the macOS job. Same shape: build, sign,
  notarise-equivalent, checksum, publish on a tag. Reuse the structure.
- `scripts/make_app.sh`: in particular the identity-selection logic, which
  refuses to guess between multiple signing certificates, and the build-time
  guard that fails when a required entitlement is missing. Windows wants the
  same posture: fail the build rather than ship something subtly wrong.
- `scripts/setup.sh`: the macOS first-run bootstrap. Windows needs an
  equivalent, or needs to make it unnecessary by bundling.
- `packaging/homebrew/lippy.rb`: the macOS distribution manifest, for
  comparison with what the Store needs.

## Pre-flight (cross-cutting, touches 3 of 10 surfaces)

- **CI/CD workflows**: a Windows release job alongside the macOS one.
- **Secrets / config**: code-signing material in repository secrets. Note that
  Store submission re-signs the package, so a Store-only path may need less
  signing material than a direct download does. Establish which before
  provisioning anything.
- **External integrations**: Microsoft Partner Center, package identity, store
  listing.

**Rollback:** a bad Windows release is deleted like any other release. The risk
is not rollback, it is publishing something signed and broken to a store where
review latency makes the fix slow. Verify the artifact by installing it before
submitting.

## Phase 1. A self-contained executable

- PyInstaller build producing a single distributable. The Python dependencies,
  the ONNX runtime and the tray shell all bundle. The 640 MB ASR model does not:
  it downloads on first run, the way the macOS build downloads its models.
- Decide and document where user data lives (`%LOCALAPPDATA%\Lippy`), and make
  sure the config migration path written for macOS still works when a Windows
  user upgrades.
- Reproduce the macOS build guard: the build must **fail** if the artifact is
  missing something that would make it silently useless. On macOS that was the
  microphone entitlement, which produced a build that looked perfect and could
  not hear. Find the Windows equivalent and guard it.
- Smoke-launch the built executable in CI and assert it starts, loads config and
  exits cleanly. A build that produces a file is not the same as a build that
  produces a program.

## Phase 2, Store packaging and release

- MSIX package with a declared identity. Reserve the name in Partner Center
  first: the manifest cannot be authored honestly with placeholder identity
  values.
- Establish whether the Store's re-signing on submission removes the need for a
  separate code-signing certificate on this path. If it does, prefer the Store
  path for the first release and treat direct download as a follow-on.
- Release workflow triggered by a version tag: build, sign if material is
  present, package, checksum, publish. Mirror the macOS job's behaviour when
  signing material is **absent**: it must still build, and say clearly in the
  output that the result is not distributable, rather than failing obscurely or,
  worse, succeeding quietly.
- A store listing needs copy, screenshots and a privacy declaration. The privacy
  answer is unusually easy here and should be stated plainly: no network
  permission, no account, no telemetry, and audio that does not leave the
  machine.

## Testing requirements

Packaging is verified by artifacts rather than unit tests, but the parts that
can be tested must be: version-string derivation, config path resolution on
Windows, and the first-run bootstrap logic.

The build guards are the real test surface. Write them so they fail loudly on a
deliberately broken input, and prove that they do. A guard nobody has seen fail
is a guard nobody knows works. This has already been demonstrated once on the
macOS side, where a signing-identity lookup aborted the whole script silently on
any machine without a certificate installed, and the fallback written for
exactly those machines could never be reached.

## Deliverables

1. A Windows executable that installs and runs on a machine that has never seen
   the source.
2. An MSIX package with a real declared identity.
3. A release workflow that produces both from a version tag, and behaves
   sensibly when signing material is absent.
4. A verified install: download the artifact the workflow published, install it
   on a real Windows machine, dictate one sentence.
5. README install instructions for Windows that do not assume a developer.

## Constraints

- Do not change the client's behaviour to suit the packaging. If packaging
  demands a behavioural change, that is a Part 1 revision with its own PR.
- Do not vendor the ASR model into the package. It downloads on first run.
- Do not publish to the Store until the artifact has been installed and used on
  a real machine. CI producing a file is not evidence that it works, and store
  review latency makes a bad first submission expensive.
- The author has no Windows machine. Deliverable 4 requires someone else, and
  the work is not done until they have done it.
