
import os
import selectors
import signal
import subprocess
import time

import yaml

from .. import base
from .. import util

class Launcher:
	pass

def lock_run(run):
	(exp, instance) = (run.experiment, run.instance.filename)
	util.try_mkdir(os.path.join(run.config.basedir, 'aux'))
	util.try_mkdir(os.path.join(run.config.basedir, 'output'))
	util.try_mkdir(exp.aux_subdir)
	util.try_mkdir(exp.output_subdir)

	# We will try to launch the experiment.
	# First, create a .lock file. If that is successful, we are the process that
	# gets to launch the experiment. Afterwards, concurrent access to our files
	# can be considered a bug (or deliberate misuse) and will lead to hard failues.
	try:
		lockfd = os.open(run.aux_file_path('lock'),
				os.O_RDONLY | os.O_CREAT | os.O_EXCL, mode=0)
	except FileExistsError:
		# TODO: Those warnings should be behind a flag.
#				print("Warning: .lock file exists for experiment '{}', instance '{}'".format(
#						exp.name, instance))
#				print("Either experiments are launched concurrently or the launcher crashed.")
		return False
	os.close(lockfd)
	return True

def create_run_file(run):
	(exp, instance) = (run.experiment, run.instance.filename)

	# Create the .run file. This signals that the run has been submitted.
	with open(run.aux_file_path('run.tmp'), "w") as f:
		pass
	os.rename(run.aux_file_path('run.tmp'), run.aux_file_path('run'))

# Stores all information that is necessary to invoke a run.
# This is a view over a POD object which can be YAML-encoded and sent
# over a wire or stored into a file.
class RunManifest:
	def __init__(self, yml):
		self.yml = yml

	@property
	def base_dir(self):
		return self.yml['config']['base_dir']

	@property
	def instance_dir(self):
		return self.yml['config']['instance_dir']

	@property
	def revision(self):
		return self.yml['revision']

	@property
	def instance(self):
		return self.yml['instance']

	@property
	def experiment(self):
		return self.yml['experiment']

	@property
	def repetition(self):
		return self.yml['repetition']

	@property
	def args(self):
		return self.yml['args']

	@property
	def environ(self):
		env_vars = self.yml['environ']
		for variant in self.yml['variants']:
			env_vars.update(variant['environ'].items())

		return env_vars

	@property
	def output(self):
		return self.yml['output']

	@property
	def timeout(self):
		return self.yml['timeout']

	@property
	def aux_subdir(self):
		return base.get_aux_subdir(self.base_dir, self.experiment,
				[var_yml['name'] for var_yml in self.yml['variants']],
				self.revision)

	@property
	def output_subdir(self):
		return base.get_output_subdir(self.base_dir, self.experiment,
				[var_yml['name'] for var_yml in self.yml['variants']],
				self.revision)

	@property
	def workdir(self):
		return self.yml['workdir']

	def aux_file_path(self, ext):
		return os.path.join(self.aux_subdir,
				base.get_aux_file_name(ext, self.instance, self.repetition))

	def output_file_path(self, ext):
		return os.path.join(self.output_subdir,
				base.get_output_file_name(ext, self.instance, self.repetition))

	def get_extra_args(self):
		extra_args = []
		for var_yml in self.yml['variants']:
			extra_args.extend(var_yml['extra_args'])
		return extra_args

	def get_paths(self):
		paths = []
		for build_yml in self.yml['builds']:
			paths.append(os.path.join(build_yml['prefix'], 'bin'))
		return paths

	def get_ldso_paths(self):
		paths = []
		for build_yml in self.yml['builds']:
			paths.append(os.path.join(build_yml['prefix'], 'lib64'))
			paths.append(os.path.join(build_yml['prefix'], 'lib'))
		return paths

	def get_python_paths(self):
		paths = []
		for build_yml in self.yml['builds']:
			for export in build_yml['exports_python']:
				paths.append(os.path.join(build_yml['prefix'], export))
		return paths

def compile_manifest(run):
	exp = run.experiment

	# Perform a DFS to discover all used builds.
	recursive_builds = []
	builds_visited = set()

	for name in exp.info.used_builds:
		assert name not in builds_visited
		build = run.config.get_build(name, exp.revision)
		recursive_builds.append(build)
		builds_visited.add(name)

	i = 0 # Need index-based loop as recursive_builds is mutated in the loop.
	while i < len(recursive_builds):
		build = recursive_builds[i]
		for req_name in build.info.requirements:
			if req_name in builds_visited:
				continue
			req_build = run.config.get_build(req_name, exp.revision)
			recursive_builds.append(req_build)
			builds_visited.add(req_name)
		i += 1

	builds_yml = []
	for build in recursive_builds:
		builds_yml.append({
			'prefix': build.prefix_dir,
			'exports_python': build.info.exports_python
		})

	# Collect extra arguments from variants
	variants_yml = []
	for variant in exp.variation:
		environ = {}
		if 'environ' in variant.variant_yml:
			for (k, v) in variant.variant_yml['environ'].items():
				environ[k] = str(v)
		variants_yml.append({
			'name': variant.name,
			'extra_args': variant.variant_yml.get('extra_args', []),
			'environ': environ
		})

	timeout = None
	if 'timeout' in exp.info._exp_yml:
		timeout = float(exp.info._exp_yml['timeout'])

	environ = {}
	if 'environ' in exp.info._exp_yml:
		for (k, v) in exp.info._exp_yml['environ'].items():
			environ[k] = str(v)

	return RunManifest({
		'config': {
			'base_dir': run.config.basedir,
			'instance_dir': run.config.instance_dir()
		},
		'experiment': exp.name,
		'variants': variants_yml,
		'revision': exp.revision.name if exp.revision else None,
		'instance': run.instance.filename,
		'repetition': run.repetition,
		'builds': builds_yml,
		'args': exp.info._exp_yml['args'],
		'timeout': timeout,
		'environ': environ,
		'output': exp.info._exp_yml.get('output', None),
		'workdir': exp.info._exp_yml.get('workdir', None)
	})

def invoke_run(manifest):
	# Create the output file. This signals that the run has been started.
	stdout = None
	with open(manifest.output_file_path('out'), "w") as f:
		# We do not actually need to write anything to the output file.
		# However, we might want to pipe experimental output to it.
		if manifest.output == 'stdout':
			stdout = os.dup(f.fileno())

	def substitute(p):
		if p == 'INSTANCE':
			return manifest.instance_dir + '/' + manifest.instance
		elif p == 'REPETITION':
			return str(manifest.repetition)
		elif p == 'OUTPUT':
			return manifest.output_file_path('out')
		elif p == 'OUTPUT_SUBDIR':
			return manifest.output_subdir
		else:
			return None

	def substitute_list(p):
		if p == 'EXTRA_ARGS':
			return manifest.get_extra_args()
		else:
			return None

	cmd = util.expand_at_params(manifest.args, substitute, listfn=substitute_list)

	# Build the environment.
	def prepend_env(var, items):
		if(var in os.environ):
			return ':'.join(items) + ':' + os.environ[var]
		return ':'.join(items)

	environ = os.environ.copy()
	environ['PATH'] = prepend_env('PATH', manifest.get_paths())
	environ['LD_LIBRARY_PATH'] = prepend_env('LD_LIBRARY_PATH', manifest.get_ldso_paths())
	environ['PYTHONPATH'] = prepend_env('PYTHONPATH', manifest.get_python_paths())
	environ.update(manifest.environ)

	# Dumps data from an FD to the FS.
	# Creates the output file only if something is written.
	class LazyWriter:
		def __init__(self, fd, path):
			self._fd = fd
			self._path = path
			self._out = None

		def progress(self):
			# Specify some chunk size to avoid reading the whole pipe at once.
			chunk = self._fd.read(16 * 1024)
			if not len(chunk):
				return False

			if self._out is None:
				self._out = open(self._path, "wb")
			self._out.write(chunk)
			return True

		def close(self):
			if self._out is not None:
				self._out.close()

	start = time.perf_counter()
	cwd = (util.expand_at_params(manifest.workdir, substitute) 
			if manifest.workdir is not None else manifest.base_dir)
	child = subprocess.Popen(cmd, cwd=cwd, env=environ,
			stdout=stdout, stderr=subprocess.PIPE)
	sel = selectors.DefaultSelector()

	stderr_writer = LazyWriter(child.stderr, manifest.aux_file_path('stderr'))
	sel.register(child.stderr, selectors.EVENT_READ, stderr_writer)

	# Wait until the run program finishes.
	while True:
		if child.poll() is not None:
			break

		elapsed = time.perf_counter() - start
		if manifest.timeout is not None and elapsed > manifest.timeout:
			child.send_signal(signal.SIGXCPU)

		# Consume any output that might be ready.
		events = sel.select(timeout=1)
		for (sk, mask) in events:
			if not sk.data.progress():
				sel.unregister(sk.fd)

	# Consume all remaining output.
	while True:
		events = sel.select(timeout=0)
		for (sk, mask) in events:
			if not sk.data.progress():
				sel.unregister(sk.fd)
		if not events:
			break
	stderr_writer.close()
	runtime = time.perf_counter() - start

	# Collect the status information.
	status = None
	sigcode = None
	if child.returncode < 0: # Killed by a signal?
		sigcode = signal.Signals(-child.returncode).name
	else:
		status = child.returncode
	did_timeout = manifest.timeout is not None and runtime > manifest.timeout

	# Create the status file to signal that we are finished.
	status_dict = {'timeout': did_timeout, 'walltime': runtime,
			'status': status, 'signal': sigcode}
	with open(manifest.output_file_path('status.tmp'), "w") as f:
		yaml.dump(status_dict, f)
	os.rename(manifest.output_file_path('status.tmp'), manifest.output_file_path('status'))

