import sys
sys.path.insert(0, r"F:\github\accesspointmg\o3de-cli")
from o3de_cli.core.resolver import Resolver

r = Resolver()
r.resolve()
gem = r.objects.get("org.o3de.gem.achievementstest")
print("gem:", gem.name, gem.version, gem.path)
print("matched overlays:", [(o.name, o.data.get("extends")) for o in gem.overlays])
