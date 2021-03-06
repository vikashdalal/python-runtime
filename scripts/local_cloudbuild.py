#!/usr/bin/env python3

# Copyright 2017 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Emulate the Google Container Builder locally.

The input is a local cloudbuild.yaml file.  This is translated into a
series of commands for the locally installed Docker daemon.  These
commands are output as a shell script and optionally executed.

The output images are not pushed to the Google Container Registry.
Not all cloudbuild.yaml functionality is supported.  In particular,
substitutions are a simplified subset that doesn't include all the
corner cases and error conditions.

See https://cloud.google.com/container-builder/docs/api/build-steps
for more information.
"""

import argparse
import collections
import collections.abc
import functools
import io
import os
import re
import shlex
import subprocess
import sys

import yaml

import validation_utils


# Exclude non-printable control characters (including newlines)
PRINTABLE_REGEX = re.compile(r"""^[^\x00-\x1f]*$""")

# Container Builder substitutions
# https://cloud.google.com/container-builder/docs/api/build-requests#substitutions
SUBSTITUTION_REGEX = re.compile(r"""(?x)
    [$]                    # Dollar sign
    (
        [A-Z_][A-Z0-9_]*   # Variable name, no curly brackets
        |
        {[A-Z_][A-Z0-9_]*} # Variable name, with curly brackets
        |
        [$]                # $$, translated to a single literal $
    )
""")

# Default builtin substitutions
DEFAULT_SUBSTITUTIONS = {
    'BRANCH_NAME': '',
    'BUILD_ID': 'abcdef12-3456-7890-abcd-ef0123456789',
    'COMMIT_SHA': '',
    'PROJECT_ID': 'dummy-project-id',
    'REPO_NAME': '',
    'REVISION_ID': '',
    'TAG_NAME': '',
}

# Use this image for cleanup actions
DEBIAN_IMAGE = 'gcr.io/google-appengine/debian8'

# File template
BUILD_SCRIPT_TEMPLATE = """\
#!/bin/bash
# This is a generated file.  Do not edit.

set -euo pipefail

SOURCE_DIR=.

# Setup staging directory
HOST_WORKSPACE=$(mktemp -d -t local_cloudbuild_XXXXXXXXXX)
function cleanup {{
    if [ "${{HOST_WORKSPACE}}" != '/' -a -d "${{HOST_WORKSPACE}}" ]; then
        # Expect a single error message about /workspace busy
        {cleanup_str} 2>/dev/null || true
        # Do not expect error messages here.  Display but ignore.
        rmdir "${{HOST_WORKSPACE}}" || true
    fi
}}
trap cleanup EXIT

# Copy source to staging directory
echo "Copying source to staging directory ${{HOST_WORKSPACE}}"
rsync -avzq --exclude=.git "${{SOURCE_DIR}}" "${{HOST_WORKSPACE}}"

# Build commands
{docker_str}
# End of build commands
echo "Build completed successfully"
"""


# Validated cloudbuild recipe + flags
CloudBuild = collections.namedtuple('CloudBuild',
                                    'output_script run steps substitutions')

# Single validated step in a cloudbuild recipe
Step = collections.namedtuple('Step', 'args dir_ env name')


def sub_and_quote(s, substitutions, substitutions_used):
    """Return a shell-escaped, variable substituted, version of the string s.

    Args:
        s (str): Any string
        subs (dict): Substitution map to apply
        subs_used (set): Updated with names from `subs.keys()` when those
                         substitutions are encountered in `s`
    """

    def sub(match):
        """Perform a single substitution."""
        variable_name = match.group(1)
        if variable_name[0] == '{':
            # Strip curly brackets
            variable_name = variable_name[1:-1]
        if variable_name == '$':
            value = '$'
        elif variable_name not in substitutions:
            # Variables must be set
            raise ValueError(
                'Variable "{}" used without being defined.  Try adding '
                'it to the --substitutions flag'.format(variable_name))
        else:
            value = substitutions.get(variable_name)
        substitutions_used.add(variable_name)
        return value

    substituted_s = re.sub(SUBSTITUTION_REGEX, sub, s)
    quoted_s = shlex.quote(substituted_s)
    return quoted_s


def get_cloudbuild(raw_config, args):
    """Read and validate a cloudbuild recipe

    Args:
        raw_config (dict): deserialized cloudbuild.yaml
        args (argparse.Namespace): command line flags

    Returns:
        CloudBuild: valid configuration
    """
    if not isinstance(raw_config, dict):
        raise ValueError(
            'Expected {} contents to be of type "dict", but found type "{}"'.
            format(args.config, type(raw_config)))

    raw_steps = validation_utils.get_field_value(raw_config, 'steps', list)
    if not raw_steps:
        raise ValueError('No steps defined in {}'.format(args.config))

    steps = [get_step(raw_step) for raw_step in raw_steps]
    return CloudBuild(
        output_script=args.output_script,
        run=args.run,
        steps=steps,
        substitutions=args.substitutions,
    )


def get_step(raw_step):
    """Read and validate a single cloudbuild step

    Args:
        raw_step (dict): deserialized step

    Returns:
        Step: valid build step
    """
    if not isinstance(raw_step, dict):
        raise ValueError(
            'Expected step to be of type "dict", but found type "{}"'.
            format(type(raw_step)))
    raw_args = validation_utils.get_field_value(raw_step, 'args', list)
    args = [validation_utils.get_field_value(raw_args, index, str)
            for index in range(len(raw_args))]
    dir_ = validation_utils.get_field_value(raw_step, 'dir', str)
    raw_env = validation_utils.get_field_value(raw_step, 'env', list)
    env = [validation_utils.get_field_value(raw_env, index, str)
           for index in range(len(raw_env))]
    name = validation_utils.get_field_value(raw_step, 'name', str)
    return Step(
        args=args,
        dir_=dir_,
        env=env,
        name=name,
    )


def generate_command(step, substitutions, substitutions_used):
    """Generate a single shell command to run for a single cloudbuild step

    Args:
        step (Step): Valid build step
        subs (dict): Substitution map to apply
        subs_used (set): Updated with names from `subs.keys()` when those
                         substitutions are encountered in an element of `step`

    Returns:
        [str]: A single shell command, expressed as a list of quoted tokens.
    """
    quoted_args = [sub_and_quote(arg, substitutions, substitutions_used)
                   for arg in step.args]
    quoted_env = []
    for env in step.env:
        quoted_env.extend(['--env', sub_and_quote(env, substitutions,
                                                  substitutions_used)])
    quoted_name = sub_and_quote(step.name, substitutions, substitutions_used)
    workdir = '/workspace'
    if step.dir_:
        workdir = os.path.join(workdir, sub_and_quote(step.dir_, substitutions,
                                                      substitutions_used))
    process_args = [
        'docker',
        'run',
        '--volume',
        '/var/run/docker.sock:/var/run/docker.sock',
        '--volume',
        '/root/.docker:/root/.docker',
        '--volume',
        '${HOST_WORKSPACE}:/workspace',
        '--workdir',
        workdir,
    ] + quoted_env + [quoted_name] + quoted_args
    return process_args


def generate_script(cloudbuild):
    """Generate the contents of a shell script

    Args:
        cloudbuild (CloudBuild): Valid cloudbuild configuration

    Returns:
        (str): Contents of shell script
    """
    # This deletes everything in /workspace including hidden files,
    # but not /workspace itself
    cleanup_step = Step(
        args=['rm', '-rf', '/workspace'],
        dir_='',
        env=[],
        name=DEBIAN_IMAGE,
    )
    cleanup_command = generate_command(cleanup_step, {}, set())
    subs_used = set()
    docker_commands = [
        generate_command(step, cloudbuild.substitutions, subs_used)
        for step in cloudbuild.steps]

    # Check that all user variables were referenced at least once
    user_subs_unused = [name for name in cloudbuild.substitutions.keys()
                        if name not in subs_used and name[0] == '_']
    if user_subs_unused:
        nice_list = '"' + '", "'.join(sorted(user_subs_unused)) + '"'
        raise ValueError(
            'User substitution variables {} were defined in the '
            '--substitution flag but never used in the cloudbuild file.'.
            format(nice_list))

    cleanup_str = ' '.join(cleanup_command)
    docker_lines = []
    for docker_command in docker_commands:
        line = ' '.join(docker_command) + '\n\n'
        docker_lines.append(line)
    docker_str = ''.join(docker_lines)

    s = BUILD_SCRIPT_TEMPLATE.format(cleanup_str=cleanup_str,
                                     docker_str=docker_str)
    return s


def make_executable(path):
    """Set executable bit(s) on file"""
    # http://stackoverflow.com/questions/12791997
    mode = os.stat(path).st_mode
    mode |= (mode & 0o444) >> 2  # copy R bits to X
    os.chmod(path, mode)


def write_script(cloudbuild, contents):
    """Write a shell script to a file."""
    print('Writing build script to {}'.format(cloudbuild.output_script))
    with io.open(cloudbuild.output_script, 'w', encoding='utf8') as outfile:
        outfile.write(contents)
    make_executable(cloudbuild.output_script)


def local_cloudbuild(args):
    """Execute the steps of a cloudbuild.yaml locally

    Args:
        args: command line flags as per parse_args
    """
    # Load and parse cloudbuild.yaml
    with io.open(args.config, 'r', encoding='utf8') as cloudbuild_file:
        raw_config = yaml.safe_load(cloudbuild_file)

    # Determine configuration
    cloudbuild = get_cloudbuild(raw_config, args)

    # Create shell script
    contents = generate_script(cloudbuild)
    write_script(cloudbuild, contents)

    # Run shell script
    if cloudbuild.run:
        print('Running {}'.format(cloudbuild.output_script))
        args = [os.path.abspath(cloudbuild.output_script)]
        subprocess.check_call(args)


def parse_args(argv):
    """Parse and validate command line flags"""
    parser = argparse.ArgumentParser(
        description='Process cloudbuild.yaml locally to build Docker images')
    parser.add_argument(
        '--config',
        type=functools.partial(
            validation_utils.validate_arg_regex, flag_regex=PRINTABLE_REGEX),
        default='cloudbuild.yaml',
        help='Path to cloudbuild.yaml file'
    )
    parser.add_argument(
        '--output_script',
        type=functools.partial(
            validation_utils.validate_arg_regex, flag_regex=PRINTABLE_REGEX),
        help='Filename to write shell script to',
    )
    parser.add_argument(
        '--no-run',
        action='store_false',
        help='Create shell script but don\'t execute it',
        dest='run',
    )
    parser.add_argument(
        '--substitutions',
        type=validation_utils.validate_arg_dict,
        default={},
        help='Parameters to be substituted in the build specification',
    )
    args = parser.parse_args(argv[1:])
    if not args.output_script:
        args.output_script = args.config + "_local.sh"
    return args


def main():
    args = parse_args(sys.argv)
    local_cloudbuild(args)


if __name__ == '__main__':
    main()
