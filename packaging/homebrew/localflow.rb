# Homebrew cask for LocalFlow.
#
# This file belongs in a tap repository (cscmsg/homebrew-tap) under Casks/,
# not here -- it lives in this repo so the version and checksum can be updated
# alongside a release. Copy it across when cutting one.
#
#   sha256: shasum -a 256 LocalFlow-<version>.dmg   (also in SHA256SUMS.txt)
# WARNING: the v0.8.0 build is ad-hoc signed, not notarised, because the Apple
# secrets are not yet present in this repository. Gatekeeper rejects it. Do not
# publish this cask to a tap until a signed release exists.
cask "localflow" do
  version "0.8.0"
  sha256 "cf7ad4d69ada5e877a23545fba3e6a3d08431f97d86789e93ec2eae0c60fa03b"

  url "https://github.com/cscmsg/localflow/releases/download/v#{version}/LocalFlow-#{version}.dmg"
  name "LocalFlow"
  desc "Local dictation with on-device transcript cleanup"
  homepage "https://github.com/cscmsg/localflow"

  # MLX is Apple Silicon only, and the app targets macOS 14+.
  depends_on arch: :arm64
  depends_on macos: ">= :sonoma"

  app "LocalFlow.app"

  caveats <<~EOS
    Before first use, run the setup script to build the Python environment and
    download the models (about 4.5 GB, once):

      "#{appdir}/LocalFlow.app/Contents/Resources/setup.sh"

    or choose "Run First-Time Setup..." from LocalFlow's menu bar item.

    LocalFlow then needs Microphone and Accessibility permission. macOS will
    ask; Accessibility is what lets the hotkey work and the text paste.
  EOS

  uninstall launchctl: "com.cscmsg.localflow.flowd",
            quit:      "com.cscmsg.localflow"

  # Deliberately not zapping the Hugging Face cache: those weights are shared
  # with any other MLX tool on the machine, and re-downloading is 4.5 GB.
  zap trash: [
    "~/Library/Application Support/LocalFlow",
    "~/Library/LaunchAgents/com.cscmsg.localflow.flowd.plist",
    "~/Library/Preferences/com.cscmsg.localflow.plist",
  ]
end
