curl -X POST "$EMBED_AND_INDEX_FUNCTION_KEY" \
  -H "Content-Type: application/json" \
  -d '{ "segments_blob": "segments/vid_130099394193495225.json", "source_url": "<original link>", "source_type": "<youtube|box|direct|upload>" }'

# localhost
# curl -X POST "http://localhost:7071/api/EmbedAndIndex" \
#   -H "Content-Type: application/json" \
#   -d '{ "segments_blob": "segments/vid_130099394193495225.json", "source_url": "<original link>", "source_type": "<youtube|box|direct|upload>" }'


