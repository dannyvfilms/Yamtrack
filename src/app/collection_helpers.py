"""Helper functions for extracting collection metadata from various sources."""

import logging

logger = logging.getLogger(__name__)


def extract_collection_metadata_from_plex(plex_metadata):
    """Extract format metadata from Plex API metadata response.

    Args:
        plex_metadata: Plex metadata dict from fetch_metadata API call

    Returns:
        Dict with normalized fields: resolution, hdr, audio_codec, audio_channels, media_type
    """
    if not plex_metadata:
        return {}

    result = {
        "resolution": "",
        "hdr": "",
        "audio_codec": "",
        "audio_channels": "",
        "bitrate": None,
        "media_type": "",
    }

    # Plex returns Media as a list in the metadata
    media_list = plex_metadata.get("Media") or []
    if not media_list:
        return result

    # Use first media item (usually there's only one)
    media = media_list[0] if isinstance(media_list, list) else media_list

    # Extract video resolution
    video_resolution = media.get("videoResolution") or media.get("videoResolution")
    if video_resolution:
        # Normalize: "1080" -> "1080p", "4k" -> "4k", "sd" -> "480p"
        resolution_map = {
            "1080": "1080p",
            "720": "720p",
            "480": "480p",
            "sd": "480p",
            "4k": "4k",
            "uhd": "4k",
        }
        result["resolution"] = resolution_map.get(
            video_resolution.lower(),
            video_resolution.lower(),
        )

    # Extract video codec (may indicate HDR)
    video_codec = media.get("videoCodec") or ""
    if video_codec:
        codec_lower = video_codec.lower()
        # HEVC/H.265 often used for HDR
        if "hevc" in codec_lower or "h265" in codec_lower:
            # Check if HDR is explicitly mentioned
            if "hdr" in codec_lower:
                if "dolby" in codec_lower or "dv" in codec_lower:
                    result["hdr"] = "Dolby Vision"
                else:
                    result["hdr"] = "HDR10"
            # May need additional logic to detect HDR from other metadata

    # Extract audio codec
    audio_codec = media.get("audioCodec") or ""
    if audio_codec:
        # Normalize codec names
        codec_map = {
            "dca": "DTS",
            "dts": "DTS",
            "truehd": "TrueHD",
            "ac3": "AC3",
            "aac": "AAC",
            "eac3": "E-AC3",
            "flac": "FLAC",
            "mp3": "MP3",
            "opus": "Opus",
            "vorbis": "Vorbis",
        }
        codec_lower = audio_codec.lower()
        result["audio_codec"] = codec_map.get(codec_lower, audio_codec)

    # Extract audio channels
    audio_channels = media.get("audioChannels") or media.get("audioChannels")
    if audio_channels:
        # Normalize: "2" -> "2.0", "6" -> "5.1", "8" -> "7.1"
        channel_map = {
            "1": "1.0",
            "2": "2.0",
            "6": "5.1",
            "8": "7.1",
        }
        channels_str = str(audio_channels)
        result["audio_channels"] = channel_map.get(channels_str, channels_str)

    # Extract bitrate (in kbps)
    bitrate = media.get("bitrate")
    if bitrate:
        try:
            result["bitrate"] = int(bitrate)
        except (TypeError, ValueError):
            result["bitrate"] = None

    # Extract container (helps determine source type)
    container = media.get("container") or ""
    if container:
        # Infer media_type from container (mkv/mp4 often digital, but not always)
        # This is a heuristic - user may need to override
        container_lower = container.lower()
        if container_lower in ("mkv", "mp4", "m4v", "avi"):
            result["media_type"] = "digital"
        # Could also check library section type if available

    # Check for 3D flag (if available in Plex metadata)
    # Plex may not have explicit 3D flag, would need to check other fields

    return result


def extract_collection_metadata_from_jellyfin(jellyfin_metadata):
    """Extract format metadata from Jellyfin API item response.

    Args:
        jellyfin_metadata: Jellyfin metadata dict from /Items/{id} API call

    Returns:
        Dict with normalized fields: resolution, hdr, audio_codec, audio_channels, media_type
    """
    if not jellyfin_metadata:
        return {}

    result = {
        "resolution": "",
        "hdr": "",
        "audio_codec": "",
        "audio_channels": "",
        "bitrate": None,
        "media_type": "",
        "is_3d": False,
    }

    # Jellyfin returns MediaStreams as an array
    media_streams = jellyfin_metadata.get("MediaStreams") or []
    if not media_streams:
        return result

    # Find video and audio streams
    video_stream = None
    audio_stream = None

    for stream in media_streams:
        stream_type = stream.get("Type", "").lower()
        if stream_type == "video" and not video_stream:
            video_stream = stream
        elif stream_type == "audio" and not audio_stream:
            audio_stream = stream

    # Extract video metadata
    if video_stream:
        width = video_stream.get("Width")
        height = video_stream.get("Height")
        if width and height:
            # Derive resolution from dimensions
            if height >= 2160:
                result["resolution"] = "4k"
            elif height >= 1080:
                result["resolution"] = "1080p"
            elif height >= 720:
                result["resolution"] = "720p"
            elif height >= 480:
                result["resolution"] = "480p"

        # Check for HDR
        video_range = video_stream.get("VideoRange", "").lower()
        if "hdr" in video_range or "dolby vision" in video_range:
            if "dolby" in video_range or "dv" in video_range:
                result["hdr"] = "Dolby Vision"
            else:
                result["hdr"] = "HDR10"

        # Check for 3D
        result["is_3d"] = video_stream.get("Is3D", False)

    # Extract audio metadata
    if audio_stream:
        codec = audio_stream.get("Codec", "")
        if codec:
            # Normalize codec names
            codec_map = {
                "dca": "DTS",
                "dts": "DTS",
                "truehd": "TrueHD",
                "ac3": "AC3",
                "aac": "AAC",
                "eac3": "E-AC3",
                "flac": "FLAC",
                "mp3": "MP3",
                "opus": "Opus",
            }
            codec_lower = codec.lower()
            result["audio_codec"] = codec_map.get(codec_lower, codec)

        channels = audio_stream.get("Channels")
        if channels:
            # Format channels: 2 -> "2.0", 6 -> "5.1", 8 -> "7.1"
            channel_map = {
                1: "1.0",
                2: "2.0",
                6: "5.1",
                8: "7.1",
            }
            result["audio_channels"] = channel_map.get(channels, str(channels))

        # Extract bitrate (in kbps) - Jellyfin provides bitrate in MediaSources
        bitrate = audio_stream.get("Bitrate")
        if not bitrate:
            # Try MediaSources for overall bitrate
            media_sources = jellyfin_metadata.get("MediaSources") or []
            if media_sources:
                bitrate = media_sources[0].get("Bitrate")
        if bitrate:
            try:
                # Jellyfin bitrate is in bps, convert to kbps
                result["bitrate"] = int(bitrate) // 1000
            except (TypeError, ValueError):
                result["bitrate"] = None

    # Extract container from MediaSources
    media_sources = jellyfin_metadata.get("MediaSources") or []
    if media_sources:
        container = media_sources[0].get("Container", "")
        if container:
            container_lower = container.lower()
            if container_lower in ("mkv", "mp4", "m4v", "avi"):
                result["media_type"] = "digital"

    return result


def extract_collection_metadata_from_emby(emby_metadata):
    """Extract format metadata from Emby API item response.

    Args:
        emby_metadata: Emby metadata dict from /Items/{id} API call

    Returns:
        Dict with normalized fields: resolution, hdr, audio_codec, audio_channels, media_type
    """
    # Emby uses the same API structure as Jellyfin (Jellyfin is a fork of Emby)
    return extract_collection_metadata_from_jellyfin(emby_metadata)


def extract_book_format_from_provider(provider_metadata, source):
    """Extract book format from provider metadata (Hardcover or OpenLibrary).

    Args:
        provider_metadata: Provider metadata dict
        source: Source name (e.g., "hardcover", "openlibrary")

    Returns:
        Normalized format string or None if not available
    """
    if not provider_metadata:
        return None

    details = provider_metadata.get("details", {})
    if not details:
        return None

    format_value = None
    if source == "hardcover":
        format_value = details.get("format")
    elif source == "openlibrary":
        format_value = details.get("physical_format")

    if not format_value:
        return None

    # Normalize format values
    format_map = {
        "hardcover": "hardcover",
        "paperback": "paperback",
        "ebook": "ebook",
        "e-book": "ebook",
        "audiobook": "audiobook",
        "audio book": "audiobook",
        "kindle": "ebook",
        "epub": "ebook",
    }

    format_lower = format_value.lower()
    return format_map.get(format_lower, format_lower)


def extract_game_platform_from_igdb(igdb_metadata, import_source=None):
    """Extract platform/store information from IGDB metadata for game collection entries.

    Args:
        igdb_metadata: IGDB metadata dict
        import_source: Optional import source (e.g., "steam")

    Returns:
        Dict with media_type (store/platform) and optionally platform info
    """
    if not igdb_metadata:
        return {}

    result = {
        "media_type": "",
    }

    # For Steam imports, set media_type to indicate Steam store
    if import_source == "steam":
        result["media_type"] = "steam"

    # Extract platforms from IGDB metadata
    details = igdb_metadata.get("details", {})
    platforms = details.get("platforms") or []
    if platforms:
        # IGDB returns list of platform names like ["PC", "PlayStation 5", "Xbox Series X|S"]
        # For now, we'll just note that platforms are available
        # In the future, could allow user to select which platform they own
        # For Steam imports, PC is typically the platform
        if import_source == "steam" and "PC" in platforms:
            # Steam games are typically PC
            pass  # media_type already set to "steam"

    return result
