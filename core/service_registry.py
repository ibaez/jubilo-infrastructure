SERVICES = {
	"gateway"      : "jubilo_gateway",
	"auth"         : "jubilo_auth",
	"auth_worker"  : "jubilo_auth_worker",
	"church"       : "jubilo_church",
	"church_worker": "jubilo_church_worker",
	"music"        : "jubilo_music",
	"music_worker" : "jubilo_music_worker",
	"meilisearch"  : "jubilo_meilisearch"
}

def resolve_services(name_list):
	if not name_list:
		return list(SERVICES.values())

	resolved_list = []
	for name in name_list:
		if name not in SERVICES.keys():
			raise Exception(f"Service '{name}' not found in service registry {list(SERVICES.keys())}.")
		
		resolved_list.append(SERVICES[name])

	return resolved_list