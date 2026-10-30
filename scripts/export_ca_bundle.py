"""Write a CA bundle = certifi roots + the Windows certificate store, for TLS-intercepting proxies.

On networks that re-sign HTTPS traffic with a corporate root CA, Python libraries that use
certifi (huggingface_hub / fastembed model downloads, urllib) fail with
CERTIFICATE_VERIFY_FAILED. Run this once and point SSL_CERT_FILE / REQUESTS_CA_BUNDLE at the
output. Not needed on normal networks or inside Docker.

    python scripts/export_ca_bundle.py data/ca-bundle.pem
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path


def main(out: Path) -> None:
    pems: list[str] = []
    try:
        import certifi

        pems.append(Path(certifi.where()).read_text(encoding="ascii"))
    except ImportError:
        pass
    if sys.platform == "win32":
        for store in ("ROOT", "CA"):
            for der, encoding, _trust in ssl.enum_certificates(store):  # type: ignore[attr-defined]
                if encoding == "x509_asn":
                    pems.append(ssl.DER_cert_to_PEM_cert(der))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(pems), encoding="ascii")
    print(f"Wrote {len(pems)} certificate blocks to {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "data/ca-bundle.pem"))
