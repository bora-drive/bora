# Homebrew formula TEMPLATE for Bora.
#
# Modeled on the homebrew-core Python virtualenv formula pattern (the same
# pattern the upstream maestral formula used; maestral itself is nowadays
# distributed only as a Homebrew *cask* for the bundled GUI app, so a current
# homebrew-core formula for it no longer exists to copy from).
#
# THIS FILE IS NOT INSTALLABLE AS-IS YET. Bora is not published to PyPI (and
# won't be for this release), so `url` below pins the tagged git repository
# directly rather than a PyPI sdist. That part is final for v0.1.0. What's
# still outstanding is the resource checksums below, which are PLACEHOLDERs
# and must be regenerated for real before this formula can be used.
#
# github.com/indirect/bora is currently PRIVATE, so this formula cannot be
# installed by anyone outside the project until the repo is made public.
#
# To finalize this formula (on a Mac, with `brew` installed):
#
#   1. Make sure your git credentials (SSH key or HTTPS token) grant read
#      access to the private github.com/indirect/bora repo — `brew` shells
#      out to `git` to fetch the tagged source, so this must work from the
#      command line first (e.g. `git ls-remote git@github.com:indirect/bora.git`).
#   2. Run `brew update-python-resources packaging/homebrew/bora.rb`. This
#      resolves Bora's full transitive dependency set (keyring,
#      desktop-notifier etc. pull in further packages beyond the direct
#      dependencies listed here) and rewrites every `resource` block below
#      with real pythonhosted.org URLs and sha256s, replacing the
#      PLACEHOLDER checksums.
#   3. Run `ruby -c packaging/homebrew/bora.rb` to sanity-check the
#      resulting syntax before committing.
#
#   * Also check the python@3.x dependency against the newest Python that
#     Bora's dependency set supports at release time (requires-python =
#     ">=3.10").

class Bora < Formula
  include Language::Python::Virtualenv

  desc "Open-source Google Drive sync client for macOS and Linux"
  homepage "https://github.com/indirect/bora"
  # Not on PyPI: pin the tagged git repo directly. Requires git credentials
  # with read access to this private repo (see header comment above).
  url "https://github.com/indirect/bora.git", using: :git, tag: "v0.1.0"
  version "0.1.0"
  license "MIT"
  head "https://github.com/indirect/bora.git", branch: "main"

  depends_on "python@3.13"

  # -- Direct dependencies from pyproject.toml ------------------------------
  # PLACEHOLDER url/sha256 values: regenerate with `brew update-python-resources`.

  resource "click" do
    url "https://files.pythonhosted.org/packages/source/c/click/click-8.2.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "desktop-notifier" do
    url "https://files.pythonhosted.org/packages/source/d/desktop-notifier/desktop_notifier-5.0.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "fasteners" do
    url "https://files.pythonhosted.org/packages/source/f/fasteners/fasteners-0.19.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "google-api-python-client" do
    url "https://files.pythonhosted.org/packages/source/g/google-api-python-client/google_api_python_client-2.100.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "google-auth" do
    url "https://files.pythonhosted.org/packages/source/g/google-auth/google_auth-2.23.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "google-auth-httplib2" do
    url "https://files.pythonhosted.org/packages/source/g/google-auth-httplib2/google_auth_httplib2-0.1.1.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "google-auth-oauthlib" do
    url "https://files.pythonhosted.org/packages/source/g/google-auth-oauthlib/google_auth_oauthlib-1.1.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "keyring" do
    url "https://files.pythonhosted.org/packages/source/k/keyring/keyring-25.0.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "keyrings.alt" do
    url "https://files.pythonhosted.org/packages/source/k/keyrings.alt/keyrings.alt-5.0.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "oauthlib" do
    url "https://files.pythonhosted.org/packages/source/o/oauthlib/oauthlib-3.2.2.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "packaging" do
    url "https://files.pythonhosted.org/packages/source/p/packaging/packaging-24.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "pathspec" do
    url "https://files.pythonhosted.org/packages/source/p/pathspec/pathspec-0.12.1.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "pyro5" do
    url "https://files.pythonhosted.org/packages/source/p/pyro5/Pyro5-5.15.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "requests" do
    url "https://files.pythonhosted.org/packages/source/r/requests/requests-2.32.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-13.7.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "setuptools" do
    url "https://files.pythonhosted.org/packages/source/s/setuptools/setuptools-70.0.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "survey" do
    url "https://files.pythonhosted.org/packages/source/s/survey/survey-5.4.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/source/t/typing-extensions/typing_extensions-4.12.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "watchdog" do
    url "https://files.pythonhosted.org/packages/source/w/watchdog/watchdog-4.0.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  resource "xattr" do
    url "https://files.pythonhosted.org/packages/source/x/xattr/xattr-1.1.0.tar.gz"
    sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
  end

  # macOS-only dependency (sys_platform == 'darwin' marker in pyproject.toml).
  on_macos do
    resource "rubicon-objc" do
      url "https://files.pythonhosted.org/packages/source/r/rubicon-objc/rubicon_objc-0.4.9.tar.gz"
      sha256 "PLACEHOLDER_REGENERATE_WITH_BREW_UPDATE_PYTHON_RESOURCES"
    end
  end

  def install
    virtualenv_install_with_resources

    # Bora's CLI is built on click, so shell completions can be generated
    # from the installed executable.
    generate_completions_from_executable(bin/"bora", shell_parameter_format: :click)
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/bora --version")
  end
end
