# Security policy

OpenTraffic sits in a signal cabinet and places calls on a traffic
controller, so a vulnerability can affect a live intersection.

## Reporting

Please **do not** open a public issue. Report privately through GitHub's
[private vulnerability reporting](https://github.com/brundige/OpenTraffic/security/advisories/new)
with a description, the affected version (`git describe`), and steps to
reproduce. You should hear back within a week.

## Scope

In scope: the inspector (login, sessions, proxy), the detector API and
health listeners, the controller link, and the deploy scripts.

Deployment practices — keeping units off the public internet, firewall
rules, VPN access — are covered in the README's Security section.
