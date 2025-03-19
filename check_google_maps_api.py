import os
import requests
import json
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

def check_api_key():
    """Check if Google Maps API key is properly configured"""
    print("Checking Google Maps API key configuration...\n")
    
    # Get API key from environment
    api_key = os.getenv('GOOGLE_MAPS_API_KEY')
    
    if not api_key:
        print("❌ ERROR: GOOGLE_MAPS_API_KEY not found in environment variables.")
        print("Please set your API key using one of these methods:")
        print("1. Create a .env file with GOOGLE_MAPS_API_KEY=your_api_key")
        print("2. Set the environment variable manually:")
        print("   • Windows: setx GOOGLE_MAPS_API_KEY \"your_api_key\"")
        print("   • Linux/Mac: export GOOGLE_MAPS_API_KEY=\"your_api_key\"")
        return False
    
    if api_key == "YOUR_API_KEY_HERE":
        print("❌ ERROR: You need to replace 'YOUR_API_KEY_HERE' with your actual Google Maps API key.")
        print("Get an API key from: https://console.cloud.google.com/google/maps-apis/credentials")
        return False
    
    # Check if API key looks valid (basic format check)
    if len(api_key) < 20:
        print(f"⚠️ Warning: API key seems too short ({len(api_key)} chars). Check if it's valid.")
    
    print(f"✅ API key found in environment (starts with {api_key[:5]}...)")
    return api_key

def test_routes_api(api_key):
    """Test the Google Maps Routes API with a simple request"""
    print("\nTesting Google Maps Routes API connection...\n")
    
    # Define test coordinates for Hobart, Tasmania
    # (using sample coordinates similar to your actual bot implementation)
    origin_lat, origin_lng = -42.872160, 147.359686
    dest_lat, dest_lng = -42.882473, 147.329588
    
    # Define the URLs to test (trying both formats)
    urls = [
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        "https://routes.googleapis.com/v2:computeRoutes"
    ]
    
    # Test both driving and transit modes
    test_modes = [
        {
            "name": "DRIVING",
            "payload": {
                "origin": {
                    "location": {
                        "latLng": {
                            "latitude": origin_lat,
                            "longitude": origin_lng
                        }
                    }
                },
                "destination": {
                    "location": {
                        "latLng": {
                            "latitude": dest_lat,
                            "longitude": dest_lng
                        }
                    }
                },
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE",
                "computeAlternativeRoutes": False,
                "languageCode": "en-US",
                "units": "METRIC"
            },
            "headers": {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs.duration,routes.legs.distanceMeters,routes.travelAdvisory"
            }
        },
        {
            "name": "TRANSIT",
            "payload": {
                "origin": {
                    "location": {
                        "latLng": {
                            "latitude": origin_lat,
                            "longitude": origin_lng
                        }
                    }
                },
                "destination": {
                    "location": {
                        "latLng": {
                            "latitude": dest_lat,
                            "longitude": dest_lng
                        }
                    }
                },
                "travelMode": "TRANSIT",
                # Using proper transit preferences
                "transitPreferences": {
                    "routingPreference": "LESS_WALKING"
                },
                "departureTime": "2023-08-01T12:00:00Z",  # Use a fixed time for testing
                "computeAlternativeRoutes": True,
                "languageCode": "en-US",
                "units": "METRIC"
            },
            "headers": {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs,routes.legs.steps,routes.legs.steps.transitDetails,routes.legs.steps.travelMode,routes.travelAdvisory"
            }
        }
    ]
    
    # Try each mode and URL format
    overall_success = False
    
    for mode in test_modes:
        print(f"\n\nTesting {mode['name']} mode:")
        mode_success = False
        
        for i, url in enumerate(urls):
            print(f"Testing URL format {i+1}: {url}")
            try:
                response = requests.post(url, json=mode['payload'], headers=mode['headers'])
                status_code = response.status_code
                
                # Print status and truncated response
                print(f"Status code: {status_code}")
                
                if status_code == 200:
                    # First check if the response is valid JSON
                    try:
                        data = response.json()
                        print(f"Raw response: {response.text[:300]}...")
                        
                        # Check if routes key exists
                        if 'routes' in data and len(data['routes']) > 0:
                            route = data['routes'][0]
                            distance = route.get('distanceMeters', 0) / 1000
                            
                            # Extract duration - handle the seconds format (e.g., "545s")
                            duration_str = route.get('duration', '')
                            if isinstance(duration_str, str) and duration_str.endswith('s'):
                                try:
                                    seconds = int(duration_str.rstrip('s'))
                                    minutes = seconds // 60
                                    seconds_remainder = seconds % 60
                                    duration_text = f"{minutes} min {seconds_remainder} sec"
                                except ValueError:
                                    duration_text = duration_str
                            else:
                                duration_obj = route.get('duration', {})
                                duration_text = duration_obj.get('text', 'unknown') if isinstance(duration_obj, dict) else str(duration_obj)
                            
                            print(f"✅ Success! Route found: {distance:.1f} km, {duration_text} travel time.")
                            
                            # For transit mode, check if we have transit details
                            if mode['name'] == "TRANSIT":
                                has_transit_steps = False
                                for leg in route.get('legs', []):
                                    for step in leg.get('steps', []):
                                        if step.get('travelMode') == 'TRANSIT':
                                            has_transit_steps = True
                                            transit_details = step.get('transitDetails', {})
                                            line = transit_details.get('line', {})
                                            route_name = line.get('shortName') or line.get('name', 'Unknown route')
                                            print(f"  🚌 Found transit route: {route_name}")
                                            break
                                    if has_transit_steps:
                                        break
                                
                                if not has_transit_steps:
                                    print("  ⚠️ No transit steps found in the route. This might be normal if no public transit is available.")
                        else:
                            print("✅ API returned success status but no routes found in response.")
                        
                        mode_success = True
                        overall_success = True
                        break
                    except json.JSONDecodeError:
                        print(f"⚠️ Response is not valid JSON: {response.text[:200]}...")
                else:
                    response_text = response.text
                    print(f"❌ Error: {response_text[:200]}...")
                    
                    # Check for specific error patterns
                    if "API_KEY_HTTP_REFERRER_BLOCKED" in response_text:
                        print("⚠️ Your API key has HTTP referrer restrictions that are blocking the request.")
                    elif "API_KEY_INVALID" in response_text:
                        print("⚠️ The API key is invalid. Please check it's correct.")
                    elif "BILLING_ACCOUNT_DISABLED" in response_text:
                        print("⚠️ Billing is disabled for your Google Cloud project.")
                    elif "API_NOT_ACTIVATED" in response_text:
                        print("⚠️ The Routes API is not activated for your project.")
                    
            except Exception as e:
                print(f"❌ Exception: {str(e)}")
        
        if not mode_success:
            print(f"❌ All tests for {mode['name']} mode failed.")
    
    if not overall_success:
        print("\n🚨 All API tests failed. Common issues:")
        print("1. API key might not have the Routes API enabled")
        print("2. Billing might not be enabled for your Google Cloud project")
        print("3. API key might have restrictions that prevent it from being used")
        print("\nHow to fix:")
        print("1. Go to https://console.cloud.google.com/google/maps-apis/apis/routes-backend.googleapis.com")
        print("2. Enable the Routes API for your project")
        print("3. Make sure billing is enabled: https://console.cloud.google.com/billing")
        print("4. Check API key restrictions: https://console.cloud.google.com/google/maps-apis/credentials")
    else:
        print("\n✅ API test successful! The Routes API is working correctly.")

if __name__ == "__main__":
    api_key = check_api_key()
    if api_key:
        test_routes_api(api_key) 