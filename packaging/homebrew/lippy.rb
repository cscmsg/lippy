# Homebrew cask for Lippy.
#
# This file belongs in a tap repository (cscmsg/homebrew-tap) under Casks/,
# not here -- it lives in this repo so the version and checksum can be updated
# alongside a release. Copy it across when cutting one.
#
#   sha256: shasum -a 256 Lippy-<version>.dmg   (also in SHA256SUMS.txt)
cask "lippy" do
  version "0.9.0"
  sha256 "3b95c34ccb246ad8e3f921d4fe2f3d4e080eb5973ed39e213b01542ab1e483f0"

  url "https://github.com/cscmsg/lippy/releases/download/v#{version}/Lippy-#{version}.dmg"
  name "Lippy"
  desc "Local dictation with on-device transcript cleanup"
  homepage "https://github.com/cscmsg/lippy"

  # MLX is Apple Silicon only, and the app targets macOS 14+.
  depends_on arch: :arm64
  depends_on macos: :sonoma

  app "Lippy.app"

  caveats <<~EOS
    Before first use, run the setup script to build the Python environment and
    download the models (about 4.5 GB, once):

      "#{appdir}/Lippy.app/Contents/Resources/setup.sh"

    or choose "Run First-Time Setup..." from Lippy's menu bar item.

    Lippy then needs Microphone and Accessibility permission. macOS will
    ask; Accessibility is what lets the hotkey work and the text paste.
  EOS

  uninstall launchctl: "com.cscmsg.lippy.lippyd",
            quit:      "com.cscmsg.lippy"

  # Deliberately not zapping the Hugging Face cache: those weights are shared
  # with any other MLX tool on the machine, and re-downloading is 4.5 GB.
  zap trash: [
    "~/Library/Application Support/Lippy",
    "~/Library/LaunchAgents/com.cscmsg.lippy.lippyd.plist",
    "~/Library/Preferences/com.cscmsg.lippy.plist",
  ]
end
