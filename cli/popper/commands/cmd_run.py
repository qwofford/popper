import os
import re
import sys

import click

import popper.cli
from popper.cli import pass_context, log
from popper.gha import WorkflowRunner
from popper.parser import Workflow
from popper import utils as pu, scm
from popper import log as logging


@click.command(
    'run', short_help='Run a workflow or action.')
@click.argument(
    'action',
    required=False
)
@click.option(
    '--wfile',
    help=(
        'File containing the definition of the workflow. '
        '[default: ./github/main.workflow OR ./main.workflow]'
    ),
    required=False,
    default=None
)
@click.option(
    '--debug',
    help=(
        'Generate detailed messages of what popper does (overrides --quiet)'),
    required=False,
    is_flag=True
)
@click.option(
    '--dry-run',
    help='Do not run the workflow, only print what would be executed.',
    required=False,
    is_flag=True
)
@click.option(
    '--log-file',
    help='Path to a log file. No log is created if this is not given.',
    required=False
)
@click.option(
    '--on-failure',
    help='Run the given action if there is a failure.',
    required=False
)
@click.option(
    '--parallel',
    help='Executes actions in stages in parallel.',
    required=False,
    is_flag=True
)
@click.option(
    '--quiet',
    help='Do not print output generated by actions.',
    required=False,
    is_flag=True
)
@click.option(
    '--reuse',
    help='Reuse containers between executions (persist container state).',
    required=False,
    is_flag=True,
)
@click.option(
    '--runtime',
    help='Specify runtime for executing the workflow [default: docker].',
    type=click.Choice(['docker', 'singularity']),
    required=False,
    default='docker'
)
@click.option(
    '--skip',
    help=('Skip the given action (can be given multiple times).'),
    required=False,
    default=list(),
    multiple=True
)
@click.option(
    '--skip-clone',
    help='Skip pulling container images (assume they exist in local cache).',
    required=False,
    is_flag=True
)
@click.option(
    '--skip-pull',
    help='Skip cloning action repositories (assume they have been cloned).',
    required=False,
    is_flag=True
)
@click.option(
    '--with-dependencies',
    help=(
        'When an action argument is given (first positional argument), '
        'execute all its dependencies as well.'
    ),
    required=False,
    is_flag=True
)
@click.option(
    '--workspace',
    help='Path to workspace folder.',
    required=False,
    show_default=False,
    hidden=True,
    default=popper.scm.get_git_root_folder()
)
@pass_context
def cli(ctx, **kwargs):
    """Runs a Github Action Workflow.

    ACTION : The action to execute from a workflow.

    By default, Popper searches for a workflow in .github/main.workflow
    or main.workflow and executes it if found.

       $ popper run

    When an action name is passed as argument, the specified action
    from .github/main.workflow or main.workflow is executed.

       $ popper run myaction

    When an action name is passed as argument and a workflow file
    is passed through the `--wfile` option, the specified action from
    the specified workflow is executed.

       $ popper run --wfile /path/to/main.workflow myaction

    Note:

    * When CI is set, popper run searches for special keywords of the form
    `popper:run[...]`. If found, popper executes with the options given in
    these run instances else popper executes all the workflows recursively.
    """
    if os.environ.get('CI') == 'true':
        # When CI is set,
        log.info('Running in CI environment...')
        popper_run_instances = parse_commit_message()
        if popper_run_instances:
            for args in get_args(popper_run_instances):
                kwargs.update(args)
                prepare_workflow_execution(**kwargs)
        else:
            # If no special keyword is found, we run all the workflows,
            # recursively.
            prepare_workflow_execution(recursive=True, **kwargs)
    else:
        # When CI is not set,
        prepare_workflow_execution(**kwargs)


def prepare_workflow_execution(recursive=False, **kwargs):
    """Set parameters for the workflow execution
    and run the workflow."""

    # Set the logging levels.
    level = 'ACTION_INFO'
    if kwargs['quiet']:
        level = 'INFO'
    if kwargs['debug']:
        level = 'DEBUG'
    log.setLevel(level)
    if kwargs['log_file']:
        logging.add_log(log, kwargs['log_file'])

    # Remove the unnecessary kwargs.
    kwargs.pop('quiet')
    kwargs.pop('debug')
    kwargs.pop('log_file')

    # Run the workflow accordingly as recursive/CI and Non-CI.
    if recursive:
        for wfile in pu.find_recursive_wfile():
            kwargs['wfile'] = wfile
            run_workflow(**kwargs)
    else:
        run_workflow(**kwargs)


def run_workflow(**kwargs):

    kwargs['wfile'] = pu.find_default_wfile(kwargs['wfile'])
    log.info('Found and running workflow at ' + kwargs['wfile'])
    # Initialize a Worklow. During initialization all the validation
    # takes place automatically.
    wf = Workflow(kwargs['wfile'])
    wf_runner = WorkflowRunner(wf)

    # Check for injected actions
    pre_wfile = os.environ.get('POPPER_PRE_WORKFLOW_PATH')
    post_wfile = os.environ.get('POPPER_POST_WORKFLOW_PATH')

    # Saving workflow instance for signal handling
    popper.cli.interrupt_params['parallel'] = kwargs['parallel']

    if kwargs['parallel']:
        if sys.version_info[0] < 3:
            log.fail('--parallel is only supported on Python3')
        log.warning("Using --parallel may result in interleaved output. "
                 "You may use --quiet flag to avoid confusion.")

    if kwargs['with_dependencies'] and (not kwargs['action']):
        log.fail('`--with-dependencies` can be used only with '
                 'action argument.')

    if kwargs['skip'] and kwargs['action']:
        log.fail('`--skip` can\'t be used when action argument '
                 'is passed.')

    on_failure = kwargs.pop('on_failure')
    wfile = kwargs.pop('wfile')

    try:
        if pre_wfile:
            pre_wf = Workflow(pre_wfile)
            pre_wf_runner = WorkflowRunner(pre_wf)
            pre_wf_runner.run(**kwargs)

        wf_runner.run(**kwargs)

        if post_wfile:
            post_wf = Workflow(post_wfile)
            pre_wf_runner = WorkflowRunner(post_wf)
            pre_wf_runner.run(**kwargs)

    except SystemExit as e:
        if (e.code != 0) and on_failure:
            kwargs['skip'] = list()
            kwargs['action'] = on_failure
            wf_runner.run(**kwargs)
        else:
            raise

    if kwargs['action']:
        log.info('Action "{}" finished successfully.'.format(kwargs['action']))
    else:
        log.info('Workflow "{}" finished successfully.'.format(wfile))


def parse_commit_message():
    """Parse `popper:run[]` keywords from head commit message.
    """
    head_commit = scm.get_head_commit()
    if not head_commit:
        return None

    msg = head_commit.message
    if 'Merge' in msg:
        log.info("Merge detected. Reading message from merged commit.")
        if len(head_commit.parents) == 2:
            msg = head_commit.parents[1].message

    if 'popper:run[' not in msg:
        return None

    pattern = r'popper:run\[(.+?)\]'
    popper_run_instances = re.findall(pattern, msg)
    return popper_run_instances


def get_args(popper_run_instances):
    """Parse the argument strings from popper:run[..] instances
    and return the args."""
    for args in popper_run_instances:
        args = args.split(" ")
        ci_context = cli.make_context('popper run', args)
        yield ci_context.params
