import click

from protect_archiver import settings


class PrimaryCommandGroup(click.Group):
    """Present the default archive operation in the top-level usage line."""

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write_usage(
            ctx.command_path,
            "[OPTIONS] [DEST]\n       COMMAND [ARGS]...",
        )


@click.group(
    cls=PrimaryCommandGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Archive UniFi Protect footage. DEST starts the primary archive sync. "
        "Run 'updl DEST --help' to see its options; the commands below provide "
        "specialized and backwards-compatible forms."
    ),
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    # Values remembered by a previous run become defaults for the options that were not
    # supplied on the command line or through the environment. Click applies a
    # default_map only after those two, which gives the precedence documented in
    # protect_archiver.settings.
    ctx.default_map = settings.as_default_map()
