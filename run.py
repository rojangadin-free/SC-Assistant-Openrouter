"""
run.py — development entry point.

    python run.py            http://localhost:8080
    python run.py --https    https://<your-lan-ip>:8443   (for phone testing)

The --https flag exists for one reason: voice input and "Add to Home screen"
both require a *secure context*. Browsers grant that to https:// and to
localhost, but NOT to http://192.168.x.x — so testing those two features on a
real phone over plain HTTP is impossible, and the symptom is a missing mic
button rather than an error. See VOICE_AND_PWA.md.

Flask generates a throwaway self-signed certificate for this, which the phone
will warn about once ("Advanced -> Proceed"). Development only; production is
served by Gunicorn behind a real certificate.
"""

import socket
import sys

from sc_assistant import create_app

app = create_app()


def _lan_ip():
    """Best-effort LAN address, for printing a URL that a phone can reach.

    'localhost' is useless on a phone, and hostname lookup often returns
    127.0.0.1 on Windows. Opening a UDP socket to a public address makes the OS
    pick the interface it would really route through; nothing is actually sent.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    use_https = "--https" in sys.argv

    if use_https:
        # 'adhoc' needs the `cryptography` package. Failing with the install
        # command is more useful than a bare ImportError traceback.
        try:
            import cryptography  # noqa: F401
        except ImportError:
            sys.exit("--https needs the cryptography package:\n"
                     "    pip install cryptography")

        port = 8443
        print(f"\n  Secure dev server (voice + install prompt enabled)")
        print(f"    this PC : https://localhost:{port}")
        print(f"    phone   : https://{_lan_ip()}:{port}")
        print("\n  The certificate is self-signed, so the phone will warn once.")
        print("  Tap Advanced -> Proceed. Accept it before testing the mic.\n")
        app.run(host="0.0.0.0", port=port, debug=True, ssl_context="adhoc")
    else:
        port = 8080
        print(f"\n  Dev server: http://localhost:{port}")
        print(f"  Voice input and the install prompt are DISABLED on "
              f"http://{_lan_ip()}:{port}")
        print("  (browsers require a secure context) — use --https to test "
              "them on a phone.\n")
        app.run(host="0.0.0.0", port=port, debug=True)
