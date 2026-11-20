"""Throwaway local CA and server certificate for developing atlas-ui on a Mac (never used on the Pi).

    python -m atlas.ui_server.devtls [--dir backend/.atlas-dev/tls] [--name atlas.lan ...]

Writes ``ca.pem`` (import/trust it in the browser if you don't want the certificate warning),
``server.pem`` (certificate chain) and ``server.key`` (mode 0600). The directory is gitignored.
On the Pi the certificate comes from the real ATLAS local CA (docs/runbooks/provisioning.md).
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
from collections.abc import Sequence
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DAYS = 365


def _name(cn: str) -> x509.Name:
    return x509.Name(
        [x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ATLAS dev"), x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    )


def make_dev_tls(directory: Path, names: Sequence[str] = ()) -> tuple[Path, Path, Path]:
    """Create (or reuse) ``ca.pem``, ``server.pem``, ``server.key`` in ``directory``."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ca_pem, cert_pem, key_pem = directory / "ca.pem", directory / "server.pem", directory / "server.key"
    if ca_pem.exists() and cert_pem.exists() and key_pem.exists():
        return ca_pem, cert_pem, key_pem
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("ATLAS dev CA"))
        .issuer_name(_name("ATLAS dev CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )  # fmt: skip
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    sans += [x509.DNSName(n) for n in names]
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=DAYS))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    key_pem.chmod(0o600)
    return ca_pem, cert_pem, key_pem


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default="backend/.atlas-dev/tls")
    parser.add_argument("--name", action="append", default=[], help="extra DNS name for the certificate")
    args = parser.parse_args(argv)
    ca, cert, key = make_dev_tls(Path(args.dir), args.name)
    print(f"CA {ca}\ncert {cert}\nkey {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
