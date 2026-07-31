#!/bin/bash
# Deploy to lbgweather. Fails loudly rather than reporting success after a
# partial transfer or a failed service restart.
set -e

# Web content and the existing loggers. Modes are inherited, as before.
rsync -Pavz index.html js *.py *.jpg *.html *.ico \
    --exclude=yarralumla_bay.html --exclude=notify_emails.txt \
    --exclude=cyc_*.py --exclude=feed_watch.py \
    --exclude='test_*.py' --exclude='fault_test_*.py' \
    lbgweather:wind/

# Service code lives OUTSIDE the DocumentRoot (~/wind is served by Apache).
# Modes are set explicitly here rather than inherited from the working copy:
# git records these files as 0644, so a fresh clone followed by a publish would
# otherwise put readable source back under the web root.
ssh lbgweather 'mkdir -p ~/lib/cyc && chmod 700 ~/lib ~/lib/cyc'
rsync -Pavz --chmod=D700,F600 \
    cyc_store.py cyc_ingest.py cyc_shadow_post.py feed_watch.py \
    test_cyc_store.py test_cyc_ingest.py test_feed_watch.py \
    fault_test_external.py fault_test_deployed.py \
    lbgweather:lib/cyc/

# Permitted without a password via /etc/sudoers.d/cyc-ingest. Not masked with
# "|| true": a failed restart means the deploy did not take effect.
ssh lbgweather 'sudo -n systemctl restart cyc-ingest'
