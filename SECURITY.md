# Security Policy

Do not commit SMTP credentials, PRTG authentication material, production databases, logs or other runtime secrets.

Use environment variables for SMTP configuration. If a credential is ever exposed in Git history, rotate it before granting broader repository access.
