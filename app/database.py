import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

url: str = os.environ.get("SUPABASE_URL")
anon_key: str = os.environ.get("SUPABASE_KEY")
service_key: str = os.environ.get("SUPABASE_SERVICE_KEY")

# Client réservé à l'inscription et à la connexion (clé anon)
supabase_auth: Client = create_client(url, anon_key)

# Client pour toutes les données de l'appli (clé service_role, uniquement côté serveur)
# Si SUPABASE_SERVICE_KEY est absente, on retombe sur la clé anon pour ne rien casser.
supabase: Client = create_client(url, service_key or anon_key)