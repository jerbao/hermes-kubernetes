"""Installation CLI entry point."""

from hermes_kubernetes.installation.cli import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit("Setup failed: " + str(error))
