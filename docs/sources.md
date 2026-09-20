# Integration references

Checked 2026-09-20. Runtime signatures were also inspected in installed `kotakneoapi==3.0.7` and `Telethon==1.45.0`. Documentation and account behavior may change; authenticated contract tests remain necessary.

- [Kotak official current Python SDK](https://github.com/Kotak-Neo/kotak-neo-python): `NeoAPI`, consumer token, TOTP login/MPIN validation, scrip master and typed async feed. The previous v2 repository is legacy. This app uses its authentication and market data APIs only.
- [Terraform S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3): shared remote state, bucket versioning and `use_lockfile` support. This project pins Terraform 1.10.5 and uses native S3 locking.
- [GitHub AWS OIDC setup](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws): temporary AWS credentials and exact subject/audience conditions. The included claim-inspection workflow avoids assuming a repository subject format.
- [Telethon documentation](https://docs.telethon.dev/en/stable/): Telegram user-session clients and message event handling.
- [Telegram API terms](https://core.telegram.org/api/terms) and [content licensing and AI terms](https://telegram.org/tos/content-licensing): review before content reuse or future model training.

No claim is made here that the provider grants automation or training rights, that the user's account has MCX API permissions, or that the provider's strategy has been identified.
