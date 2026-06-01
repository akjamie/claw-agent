import argparse
import sys

from cli.config import get_env_path
from dotenv import load_dotenv

# Load ~/.claw/.env at process startup — makes all project env vars
# available via os.environ globally. No need to read .env in individual modules.
load_dotenv(get_env_path())

from cli import __version__, __release_date__
from cli.models_cmd import run_models_command, show_current_model
from cli.skills_cmd import run_skills_command
from cli.gateway_cmd import register_gateway_parser, run_gateway_command
from cli.chat_cmd import register_chat_parser, run_chat_command


def main():
    parser = argparse.ArgumentParser(prog="claw", description="Claw CLI")

    # subcommands
    subparsers = parser.add_subparsers(dest="command")

    # version command
    subparsers.add_parser("version", help="Show version information")

    # models command
    models_parser = subparsers.add_parser("models", help="Configure model and provider")
    models_parser.add_argument(
        "--show",
        action="store_true",
        help="Show current model configuration"
    )

    # skills command
    skills_parser = subparsers.add_parser("skills", help="List and view bundled skills")
    skills_sub = skills_parser.add_subparsers(dest="skills_action")
    skills_sub.add_parser("list", help="List all available skills")
    skills_show = skills_sub.add_parser("show", help="Show a skill's content")
    skills_show.add_argument("name", help="Skill name")

    # gateway command
    register_gateway_parser(subparsers)

    # chat command
    register_chat_parser(subparsers)

    # Allow --version as a flag too
    parser.add_argument("--version", "-v", action="store_true", help="Show version information")

    args = parser.parse_args()

    if args.command == "version" or args.version:
        print(f"{__version__} ({__release_date__})")
    elif args.command == "models":
        if args.show:
            show_current_model()
        else:
            success = run_models_command()
            sys.exit(0 if success else 1)
    elif args.command == "skills":
        success = run_skills_command(args)
        sys.exit(0 if success else 1)
    elif args.command == "gateway":
        success = run_gateway_command(args)
        sys.exit(0 if success else 1)
    elif args.command == "chat":
        run_chat_command(args)  # exits internally with the right code
    elif args.command is None:
        parser.print_help()

if __name__ == "__main__":
    main()
