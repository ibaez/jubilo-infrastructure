import os
from core.shell import run
from core.service_registry import SERVICES, resolve_services
from secrets import token_urlsafe, choice
import string

def _read_env_file(path):
	"""Returns (values dict, key order) for an existing .env file, or
	empty/empty if it doesn't exist yet. Lines that aren't KEY=VALUE
	(blank lines, comments) are ignored -- none of the generated files use
	either today, and any manually-managed keys always take this form."""
	env = {}
	order = []

	if os.path.exists(path):
		with open(path, "r") as f:
			for line in f:
				line = line.rstrip("\n")
				if not line or line.startswith("#") or "=" not in line:
					continue
				key, _, value = line.partition("=")
				env[key] = value
				order.append(key)

	return env, order

def _update_env_file(path, updates):
	"""
	Merges `updates` into the .env file at `path` instead of overwriting
	it wholesale -- any key this script doesn't manage (R2 credentials,
	anything else added by hand) survives untouched. Existing managed keys
	are updated in place; new ones are appended at the end.
	"""
	env, order = _read_env_file(path)

	for key, value in updates.items():
		if key not in env:
			order.append(key)
		env[key] = value

	with open(path, "w") as f:
		for key in order:
			f.write(f"{key}={env[key]}\n")

def docker_generate_auth_service_env_file(docker_host_ip, tokens):
	_update_env_file("../jubilo-auth/.env", {
		"DEBUG": "True",
		"SECRET_KEY": tokens['auth']['secret_key'],
		"DJANGO_SUPERUSER_EMAIL": tokens['auth']['DJANGO_SUPERUSER_EMAIL'],
		"DJANGO_SUPERUSER_PASSWORD": tokens['auth']['DJANGO_SUPERUSER_PASSWORD'],
		"DJANGO_SUPERUSER_FIRST_NAME": tokens['auth']['DJANGO_SUPERUSER_FIRST_NAME'],
		"DJANGO_SUPERUSER_LAST_NAME": tokens['auth']['DJANGO_SUPERUSER_LAST_NAME'],
		"JUBILO_GATEWAY_IP": docker_host_ip,
		"DATABASE_URL": "postgres://jubilo_auth_user:jubilo_auth_password@jubilo_auth_db:5432/jubilo_auth",
		"REDIS_URL": "redis://jubilo_redis:6379/0",
		"REDIS_QUEUE_URL": "redis://jubilo_redis:6379/2",
		"EMAIL_HOST": "jubilo_mailpit",
		"EMAIL_PORT": "1025",
		"EMAIL_USE_TLS": "False",
		# Mailpit doesn't check credentials -- these just need to be
		# non-empty to satisfy REQUIRED_ENV_VARS.
		"EMAIL_HOST_USER": "mailpit",
		"EMAIL_HOST_PASSWORD": "mailpit",
		"RESEND_API_KEY": "unused-in-dev",
		"DEFAULT_FROM_EMAIL": "invites@mijubilo.com",
		"JUBILO_CHURCH_CLIENT_ID": tokens['church']['client_id'],
		"JUBILO_CHURCH_CLIENT_SECRET": tokens['church']['client_secret'],
		"JUBILO_MOBILE_CLIENT_ID": tokens['jubilo_mobile']['client_id'],
		"JUBILO_MOBILE_CLIENT_SECRET": tokens['jubilo_mobile']['client_secret'],
		"JUBILO_MUSIC_CLIENT_ID": tokens['jubilo_music']['client_id'],
		"JUBILO_MUSIC_CLIENT_SECRET": tokens['jubilo_music']['client_secret'],
		"JUBILO_POSTMAN_CLIENT_ID": tokens['jubilo_postman']['client_id'],
		"JUBILO_POSTMAN_CLIENT_SECRET": tokens['jubilo_postman']['client_secret'],
	})

def docker_generate_music_service_env_file(docker_host_ip, tokens):
	_update_env_file("../jubilo-music/.env", {
		"DEBUG": "True",
		"SECRET_KEY": tokens['secret_key'],
		"JUBILO_GATEWAY_IP": docker_host_ip,
		"AUTH_SERVICE_INTROSPECTION_URL": "http://jubilo-auth:8000/auth/o/introspect",
		"JUBILO_MUSIC_CLIENT_ID": tokens['client_id'],
		"JUBILO_MUSIC_CLIENT_SECRET": tokens['client_secret'],
		"DATABASE_URL": "postgres://jubilo_music_user:jubilo_music_password@jubilo_music_db:5432/jubilo_music",
		"REDIS_URL": "redis://jubilo_redis:6379/1",
		"REDIS_QUEUE_URL": "redis://jubilo_redis:6379/3",
		"JUBILO_MEILISEARCH_MASTER_KEY": tokens['JUBILO_MEILISEARCH_MASTER_KEY'],
		"JUBILO_MEILISEARCH_URL": tokens['JUBILO_MEILISEARCH_URL'],
		# R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME /
		# R2_ENDPOINT_URL are deliberately NOT managed here -- they're
		# externally-provisioned Cloudflare credentials this script has no
		# way to generate, so whatever's already in the file for them is
		# left alone.
	})

def docker_generate_church_service_env_file(docker_host_ip, tokens):
	_update_env_file("../jubilo-church/.env", {
		"JUBILO_GATEWAY_IP": docker_host_ip,
		"JUBILO_CHURCH_CLIENT_ID": tokens['client_id'],
		"JUBILO_CHURCH_CLIENT_SECRET": tokens['client_secret'],
	})

def docker_generate_jubilo_mobile_env_file(docker_host_ip, tokens):
	_update_env_file("../jubilo-mobile/.env", {
		"JUBILO_GATEWAY_IP": docker_host_ip,
		"EXPO_PUBLIC_JUBILO_AUTH_BASE_URL": f"https://{docker_host_ip}/auth",
		"EXPO_PUBLIC_JUBILO_MOBILE_CLIENT_ID": tokens['client_id'],
		"EXPO_PUBLIC_JUBILO_MUSIC_BASE_URL": f"https://{docker_host_ip}/api/music",
	})

def generate_token(length):
	while True:
		token = token_urlsafe(length)

		#------------------------------
		# Ensure the token does not start with a hyphen, as that can cause issues when used in environment variables or command-line arguments
		# 
		if not token.startswith("-"):
			return token[:length]
		
def generate_secret(length):
	chars = string.ascii_letters + string.digits
	secret_key = ''.join(choice(chars) for _ in range(length))
	return secret_key

def docker_generate_service_tokens():
	return {
		"client_id": generate_token(32),
		"client_secret": generate_token(96),
		"secret_key": generate_secret(100),
	}

def docker_generate_jubilo_infrastructure_env_files(ip):
	#------------------------------
	# This is the top level .env file that docker compose will read, and it will be used to set the DOCKER_HOST_IP environment variable for all the services in the stack.
	# This is necessary because the services in the stack need to know the IP address of the Docker host machine in order to make requests to each other, since they will be running in separate containers and won't be able to use "localhost" to refer to the host machine.
	#
	with open(".env", "w") as f:
		f.write(f"DOCKER_HOST_IP={ip}\n")

def docker_generate_ssl_certs(ip):
	print("Generating SSL certificates for Docker containers...")

	# mkcert auto-detects `keytool` on PATH and also tries to install its
	# CA into a Java trust store -- on machines where /usr/bin/keytool's
	# stub doesn't resolve to the actual JDK the same way JAVA_HOME does,
	# that step fails ("Keystore file does not exist") and aborts the
	# whole command. None of these services run in a JVM, so there's
	# nothing to install there -- restrict mkcert to the system store
	# (what the Django containers/gateway/browser actually rely on)
	# instead of trying to fix the local keytool/JAVA_HOME mismatch.
	env = os.environ.copy()
	env["TRUST_STORES"] = "system"

	run(f"mkdir -p certs && mkcert -cert-file ./certs/jubilo.pem -key-file ./certs/jubilo-key.pem {ip} localhost 127.0.0.1", process_env=env)
	print("SSL certificates generated successfully.")

def docker_get_host_ip():
	# Get the IP address of the Docker host machine: MAC_IP=$(ifconfig en0 | awk '/inet / { print $2 }')
	result = run("ifconfig en0 | awk '/inet / { print $2 }'", capture_output=True)
	return result.strip()

def docker_build(service_name_list):
	print("Building Docker containers...")
	run("docker compose build")
	print("Docker containers built successfully.")

def docker_down(service_name_list):
	service_list = resolve_services(service_name_list)

	print(f"Stopping entire Júbilo stack")
	run(f"docker compose down")
	print(f"Stopped entire Júbilo stack")

def docker_stop(service_name_list):
	service_list = resolve_services(service_name_list)

	print(f"Stopping containers: {service_list}")
	run(f"docker compose stop {' '.join(service_list)}")
	print(f"Container stopped: {service_list}")

def docker_start(service_name_list):
	service_list = resolve_services(service_name_list)

	print(f"Starting containers: {service_list}")
	run(f"docker compose up -d {' '.join(service_list)}")
	print(f"Container started: {service_list}")

def docker_run(service_name, cmd, env=None):
	service_list = resolve_services([service_name])
	resolved_service_name = service_list[0]

	# ------------------------------
	# Part 1: Copy the current host process environment
	# so we can pass it to the docker compose command.
	#
	process_env = os.environ.copy()
	env_flags = ''
	if env:
		# ------------------------------
		# Part 2: Add the env vars to the host process environment
		# so that the docker compose command can see them when it runs.
		#
		process_env.update(env)

		# ------------------------------
		# Part 3: Add -e flags to the docker compose run command
		# so that Docker knows to pull these vars from the host
		# process environment and inject them into the container.
		# Note: We pass just the key (no value) because Docker will
		# read the value from the host process environment (Part 2).
		#
		env_flags = ' '.join(f"-e {k}" for k in env.keys())

	full_cmd = f"docker compose run --rm {env_flags} {resolved_service_name} {cmd}"
	print(f"Running command in container {resolved_service_name}: {cmd}")
	run(full_cmd, process_env=process_env)
	print(f"Command executed successfully in container {resolved_service_name}")