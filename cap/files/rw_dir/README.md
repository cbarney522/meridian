Writable data for Meridian on the reader (/cust/rw_dir):

  config.json(.bak)   settings, one revisioned section per page (usb, badges, storage, ...)
  auth.json(.bak)     accounts (PBKDF2 hashes + role)
  tls/                per-reader self-signed HTTPS certificate
  log/meridian.log    server log (rotated at 1 MB)
  diag.txt            startup diagnostics (what this reader's CAP runtime can access)
