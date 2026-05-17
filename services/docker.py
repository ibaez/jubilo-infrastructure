from core.shell import run
from core.service_registry import SERVICES, resolve_services
from secrets import token_urlsafe

def docker_generate_auth_service_env_file(docker_host_ip, tokens):
	with open("../iece-auth/.env", "w") as f:
		f.write(f"DJANGO_SUPERUSER_EMAIL={tokens['auth']['DJANGO_SUPERUSER_EMAIL']}\n")
		f.write(f"DJANGO_SUPERUSER_PASSWORD={tokens['auth']['DJANGO_SUPERUSER_PASSWORD']}\n")
		f.write(f"DJANGO_SUPERUSER_FIRST_NAME={tokens['auth']['DJANGO_SUPERUSER_FIRST_NAME']}\n")
		f.write(f"DJANGO_SUPERUSER_LAST_NAME={tokens['auth']['DJANGO_SUPERUSER_LAST_NAME']}\n")
		f.write(f"IECE_GATEWAY_IP={docker_host_ip}\n")
		f.write(f"IECE_AUTH_CLIENT_ID={tokens['auth']['client_id']}\n")
		f.write(f"IECE_AUTH_CLIENT_SECRET={tokens['auth']['client_secret']}\n")
		f.write(f"IECE_CHURCH_CLIENT_ID={tokens['church']['client_id']}\n")
		f.write(f"IECE_CHURCH_CLIENT_SECRET={tokens['church']['client_secret']}\n")
		f.write(f"JUBILO_MOBILE_CLIENT_ID={tokens['jubilo_mobile']['client_id']}\n")
		f.write(f"JUBILO_MOBILE_CLIENT_SECRET={tokens['jubilo_mobile']['client_secret']}\n")
		f.write(f"JUBILO_MUSIC_CLIENT_ID={tokens['jubilo_music']['client_id']}\n")
		f.write(f"JUBILO_MUSIC_CLIENT_SECRET={tokens['jubilo_music']['client_secret']}\n")

def docker_generate_music_service_env_file(docker_host_ip, tokens):
	with open("../jubilo-music/.env", "w") as f:
		f.write(f"IECE_GATEWAY_IP={docker_host_ip}\n")
		f.write(f"JUBILO_MUSIC_CLIENT_ID={tokens['client_id']}\n")
		f.write(f"JUBILO_MUSIC_CLIENT_SECRET={tokens['client_secret']}\n")

def docker_generate_church_service_env_file(docker_host_ip, tokens):
	with open("../iece-church/.env", "w") as f:
		f.write(f"IECE_GATEWAY_IP={docker_host_ip}\n")
		f.write(f"IECE_CHURCH_CLIENT_ID={tokens['client_id']}\n")
		f.write(f"IECE_CHURCH_CLIENT_SECRET={tokens['client_secret']}\n")

def docker_generate_jubilo_mobile_env_file(docker_host_ip, tokens):
	with open("../jubilo-mobile/.env", "w") as f:
		f.write(f"IECE_GATEWAY_IP={docker_host_ip}\n")
		f.write(f"IECE_CHURCH_CLIENT_ID={tokens['client_id']}\n")
		f.write(f"IECE_CHURCH_CLIENT_SECRET={tokens['client_secret']}\n")

def generate_token(length):
	while True:
		token = token_urlsafe(length)

		#------------------------------
		# Ensure the token does not start with a hyphen, as that can cause issues when used in environment variables or command-line arguments
		# 
		if not token.startswith("-"):
			return token[:length]

def docker_generate_service_tokens():

	return {
		"client_id": generate_token(32),
		"client_secret": generate_token(96)
	}

def docker_generate_top_level_env_files(ip):
	#------------------------------
	# This is the top level .env file that docker compose will read, and it will be used to set the DOCKER_HOST_IP environment variable for all the services in the stack.
	# This is necessary because the services in the stack need to know the IP address of the Docker host machine in order to make requests to each other, since they will be running in separate containers and won't be able to use "localhost" to refer to the host machine.
	#
	with open(".env", "w") as f:
		f.write(f"DOCKER_HOST_IP={ip}\n")

def docker_generate_ssl_certs(ip):
	print("Generating SSL certificates for Docker containers...")
	run(f"mkdir -p certs && mkcert -cert-file ./certs/jubilo.pem -key-file ./certs/jubilo-key.pem {ip} localhost 127.0.0.1")
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

def docker_run(service_name, cmd):
	service_list = resolve_services([service_name])
	resolved_service_name = service_list[0]

	print(f"Running command in container {resolved_service_name}: {cmd}")
	run(f"docker compose run --rm {resolved_service_name} {cmd}")
	print(f"Command executed successfully in container {resolved_service_name}")