# Contributing to OpenTraffic

Thanks for helping. OpenTraffic drives real traffic signals, so changes
are judged first on whether they are safe and verifiable.

## AI Disclosure
This project uses anthropic model opus 4.5 to generate documentaion, perform tests, and to bootstrap high level architecture from business requirements. Functional implementation, code reviews, and code specific to the domain was not AI generated. 

## Reporting a problem

Open an issue with:

- what you ran (profile, `git describe`, Mac or Jetson / JetPack version)
- the sensor, adapter and controller involved (model and firmware)
- what you expected and what happened, with logs
  (`docker logs traffic-detector`, or the terminal in dev)

Leave out anything site-specific you would not publish: passwords, health
tokens, real intersection addresses on a city network.

Security problems go to [SECURITY.md](SECURITY.md), not the issue tracker.

## Changes

1. Fork, branch from `main`, keep each pull request to one change.
2. Run it: dev profile on a Mac (`python main.py`), or replay a clip
   (`python main.py --source data/clips/<clip>.npz --realtime`) if you
   have no sensor.
3. Say in the pull request how you tested it, and on what hardware.
   Anything that touches the controller link (`controllers/`) needs a
   bench test against a real or simulated controller, described in the
   PR.
4. Update the README when behaviour, configuration or deployment changes.

Match the surrounding code: plain Python, small modules, comments that
say why. Never commit anything from `data/`.

## License

By contributing you agree that your contribution is licensed under the
GNU General Public License v3.0 or later, the same as the project.
