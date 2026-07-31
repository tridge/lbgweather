rsync -Pavz index.html js *.py *.jpg *.html *.ico --exclude=yarralumla_bay.html --exclude=notify_emails.txt lbgweather:wind/

# Restart the ingest receiver so a deploy actually takes effect.
# Permitted without a password via /etc/sudoers.d/cyc-ingest.
ssh lbgweather 'sudo -n systemctl restart cyc-ingest' || true
