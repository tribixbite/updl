import click

from protect_archiver import settings


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    # Values remembered by a previous run become defaults for the options that were not
    # supplied on the command line or through the environment. Click applies a
    # default_map only after those two, which gives the precedence documented in
    # protect_archiver.settings.
    ctx.default_map = settings.as_default_map()
