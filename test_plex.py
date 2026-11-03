import os
from dotenv import load_dotenv
from plexapi.server import PlexServer
from plexapi.exceptions import Unauthorized

# Load environment variables from your .env file
load_dotenv()

PLEX_URL = os.getenv("PLEX_URL", "http://host.docker.internal:32400") # Or http://localhost:32400 if running on host
PLEX_TOKEN = os.getenv("PLEX_TOKEN")

def test_plex_connection():
    print(f"Testing connection to Plex server at: {PLEX_URL}...")
    
    if not PLEX_TOKEN or PLEX_TOKEN == "your_plex_token_here":
        print("[ERROR] PLEX_TOKEN is not set in your .env file!")
        return

    try:
        # Initialize PlexServer connection
        plex = PlexServer(PLEX_URL, PLEX_TOKEN)
        
        print("\n[SUCCESS] Successfully authenticated with Plex!")
        print(f"Server Name : {plex.friendlyName}")
        print(f"Version     : {plex.version}")
        print(f"Platform    : {plex.platform} ({plex.platformVersion})")
        
        # List available library sections
        print("\nAvailable Libraries:")
        sections = plex.library.sections()
        if sections:
            for section in sections:
                print(f" - {section.title} (Type: {section.type})")
        else:
            print(" - (Your libraries are currently empty — ready for new downloads!)")

    except Unauthorized:
        print("[ERROR] Authentication failed! Check your X-Plex-Token in the .env file.")
    except Exception as e:
        print(f"[ERROR] Could not connect to Plex server: {e}")

if __name__ == "__main__":
    test_plex_connection()