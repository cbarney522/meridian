"""TLS certificate for the reader's HTTPS server (from custom_ui_cap).

A per-reader self-signed certificate is generated with the reader's openssl on first start and
kept in rw_dir/tls. If openssl isn't available, the build-time certificate bundled in the CAP is
used instead (same key on every reader built from that CAP, so only as a fallback).
"""
import logging
import os
import shutil
import socket
import subprocess

log = logging.getLogger("meridian.tls")


def ensure_tls(data_dir, bundled_dir):
    tls_dir = os.path.join(data_dir, "tls")
    cert, key = os.path.join(tls_dir, "cert.pem"), os.path.join(tls_dir, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    openssl = shutil.which("openssl")
    if openssl:
        try:
            os.makedirs(tls_dir, exist_ok=True)
            cn = socket.gethostname() or "r700"
            subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
                            "-keyout", key, "-out", cert, "-subj", f"/CN={cn}"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            os.chmod(key, 0o600)
            log.info("generated self-signed TLS certificate for CN=%s", cn)
            return cert, key
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("openssl certificate generation failed: %s", e)
    b_cert, b_key = os.path.join(bundled_dir, "cert.pem"), os.path.join(bundled_dir, "key.pem")
    if os.path.exists(b_cert) and os.path.exists(b_key):
        log.warning("using the build-time bundled TLS certificate")
        return b_cert, b_key
    return None, None
