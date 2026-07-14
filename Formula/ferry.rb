class Ferry < Formula
  desc "Connect to free VPN Gate servers from a terminal TUI"
  homepage "https://github.com/araidz/Ferry"
  url "https://github.com/araidz/Ferry/archive/refs/tags/v0.3.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  depends_on "openvpn"
  depends_on "python@3.14"

  def install
    libexec.install "ferry"
    (bin/"ferry").write <<~SH
      #!/bin/sh
      export PYTHONPATH="#{libexec}:$PYTHONPATH"
      exec "#{formula_opt_bin("python@3.14")}/python3.14" -m ferry "$@"
    SH
  end

  test do
    assert_match "terminal", shell_output("#{bin}/ferry --help")
  end
end
