# MegaKernel Codex session backup

The archive is encrypted with OpenSSL CMS (AES-256-GCM). Obtain the private key separately. The key decrypts the full chat history, so keep it out of this repository.

Use OpenSSL 3 and Python 3 to restore:

```sh
openssl cms -decrypt -binary -inform DER \
  -in megakernel-session.tar.gz.cms \
  -inkey /path/to/megakernel-session-private.pem \
  -out megakernel-session.tar.gz
tar -xzf megakernel-session.tar.gz
python3 megakernel_session.py relocate ./megakernel-session
CODEX_HOME="$PWD/megakernel-session" codex login
CODEX_HOME="$PWD/megakernel-session" codex resume \
  -C /path/to/project 01a0180e-ab9d-72b1-8a0e-24db65f200fa
```
