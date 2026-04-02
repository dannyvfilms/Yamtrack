import requests
from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.db.models import Prefetch, Q

from app.models import Item, MediaTypes, Sources
from app.providers import services
from lists import smart_rules


class CustomListManager(models.Manager):
    """Manager for custom lists."""

    def get_user_lists(self, user):
        """Return the custom lists that the user owns or collaborates on."""
        return (
            self.filter(Q(owner=user) | Q(collaborators=user))
            .select_related("owner")
            .prefetch_related(
                "collaborators",
                Prefetch(
                    "items",
                    queryset=Item.objects.order_by("-customlistitem__date_added"),
                ),
                Prefetch(
                    "customlistitem_set",
                    queryset=CustomListItem.objects.order_by("-date_added"),
                ),
            )
            .distinct()
        )

    def get_user_lists_with_item(self, user, item):
        """Return user lists with item membership status."""
        return (
            self.filter(Q(owner=user) | Q(collaborators=user))
            .annotate(
                has_item=models.Exists(
                    CustomListItem.objects.filter(
                        custom_list_id=models.OuterRef("id"),
                        item=item,
                    ),
                ),
            )
            .prefetch_related("collaborators")
            .distinct()
            .order_by("name")
        )

    def get_public_list(self, list_id):
        """Return a public list by ID."""
        return (
            self.filter(id=list_id, visibility="public")
            .select_related("owner")
            .prefetch_related("collaborators")
            .first()
        )


class CustomList(models.Model):
    """Model for custom lists."""

    SOURCE_CHOICES = [
        ("local", "Local"),
        ("trakt", "Trakt"),
    ]

    VISIBILITY_CHOICES = [
        ("public", "Public"),
        ("private", "Private"),
    ]

    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    tags = models.JSONField(
        blank=True,
        default=list,
        help_text="Optional tags used to group public lists.",
    )
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    collaborators = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="collaborated_lists",
        blank=True,
    )
    items = models.ManyToManyField(
        Item,
        related_name="custom_lists",
        blank=True,
        through="CustomListItem",
    )
    visibility = models.CharField(
        max_length=10,
        choices=VISIBILITY_CHOICES,
        default="private",
    )
    allow_recommendations = models.BooleanField(
        default=False,
        help_text="Allow anyone to recommend items to add to this list (only for public lists)",
    )
    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default="local",
    )
    source_id = models.CharField(max_length=100, blank=True, default="")
    is_smart = models.BooleanField(default=False)
    smart_media_types = models.JSONField(
        blank=True,
        default=list,
        help_text="Media types included in this smart list.",
    )
    smart_excluded_media_types = models.JSONField(
        blank=True,
        default=list,
        help_text="Media types excluded from this smart list.",
    )
    smart_filters = models.JSONField(
        blank=True,
        default=dict,
        help_text="Saved filter criteria for smart lists.",
    )

    objects = CustomListManager()

    class Meta:
        """Meta options for the model."""

        ordering = ["name"]

    def __str__(self):
        """Return the name of the custom list."""
        return self.name

    def user_can_view(self, user):
        """Check if the user can view the list."""
        # Public lists are viewable by anyone
        if self.visibility == "public":
            return True
        if not user or not user.is_authenticated:
            return False
        # Private lists are only viewable by owner or collaborators
        return self.owner == user or user in self.collaborators.all()

    def user_can_edit(self, user):
        """Check if the user can edit the list."""
        if not user or not user.is_authenticated:
            return False
        return self.owner == user or user in self.collaborators.all()

    def user_can_delete(self, user):
        """Check if the user can delete the list."""
        if not user or not user.is_authenticated:
            return False
        return self.owner == user

    @property
    def is_public(self):
        """Return whether the list is public."""
        return self.visibility == "public"

    def can_recommend(self):
        """Check if recommendations are allowed for this list."""
        return self.visibility == "public" and self.allow_recommendations

    def get_smart_items_queryset(self):
        """Build a queryset of items that match this smart list definition."""
        if not self.is_smart:
            return Item.objects.none()
        normalized_rules = smart_rules.normalize_list_rules(self)

        matched_item_ids = smart_rules.collect_matching_item_ids(self.owner, normalized_rules)
        return Item.objects.filter(id__in=matched_item_ids)

    def sync_smart_items(self):
        """Synchronize list membership for smart lists."""
        if not self.is_smart:
            return

        target_item_ids = set(self.get_smart_items_queryset().values_list("id", flat=True))
        existing_item_ids = set(
            CustomListItem.objects.filter(custom_list=self).values_list("item_id", flat=True),
        )

        to_remove = existing_item_ids - target_item_ids
        to_add = target_item_ids - existing_item_ids

        if to_remove:
            CustomListItem.objects.filter(custom_list=self, item_id__in=to_remove).delete()
        if to_add:
            CustomListItem.objects.bulk_create(
                [
                    CustomListItem(custom_list=self, item_id=item_id, added_by=self.owner)
                    for item_id in to_add
                ],
            )

    @property
    def image(self):
        """Return the image of the first item in the list.
        
        For TMDB movies and TV shows, prefer horizontal backdrop image
        over the 2:3 poster for better display in list cards.
        For IGDB games, prefer widescreen screenshots or artworks over cover art.
        """
        first_item = None
        prefetched_list_items = getattr(self, "_prefetched_objects_cache", {}).get(
            "customlistitem_set",
        )
        if prefetched_list_items:
            first_item = prefetched_list_items[0].item
        if first_item is None:
            first_item = self.items.first()
        if not first_item:
            return settings.IMG_NONE
        
        # For TMDB movies and TV shows, try to get backdrop image
        if (
            first_item.source == Sources.TMDB.value
            and first_item.media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value)
        ):
            try:
                backdrop_url = self._get_tmdb_backdrop(
                    first_item.media_type,
                    first_item.media_id,
                )
                if backdrop_url and backdrop_url != settings.IMG_NONE:
                    return backdrop_url
            except Exception:
                # If anything fails, fall back to regular poster
                pass
        
        # For IGDB games, try to get widescreen artwork or screenshot
        if (
            first_item.source == Sources.IGDB.value
            and first_item.media_type == MediaTypes.GAME.value
        ):
            try:
                import logging
                logger = logging.getLogger(__name__)
                logger.debug(
                    "Attempting to get IGDB backdrop for list cover: game_id=%s, item_id=%s",
                    first_item.media_id,
                    first_item.id,
                )
                backdrop_url = self._get_igdb_backdrop(first_item.media_id)
                if backdrop_url and backdrop_url != settings.IMG_NONE:
                    logger.debug(
                        "Using IGDB backdrop for list cover: %s",
                        backdrop_url,
                    )
                    return backdrop_url
                else:
                    logger.debug(
                        "No IGDB backdrop found, falling back to cover art for game %s",
                        first_item.media_id,
                    )
            except Exception as exc:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(
                    "Error getting IGDB backdrop for list cover: %s",
                    exc,
                    exc_info=True,
                )
                # If anything fails, fall back to regular cover
                pass
        
        # Fall back to regular poster image
        return first_item.image
    
    def _get_tmdb_backdrop(self, media_type, media_id):
        """Get backdrop image URL from TMDB for movies and TV shows.
        
        Uses caching to avoid repeated API calls for the same item.
        """
        cache_key = f"tmdb_backdrop_{media_type}_{media_id}"
        cached_backdrop = cache.get(cache_key)
        if cached_backdrop is not None:
            return cached_backdrop
        
        try:
            from app.providers import tmdb
            
            if media_type == MediaTypes.MOVIE.value:
                url = f"{tmdb.base_url}/movie/{media_id}"
            else:
                url = f"{tmdb.base_url}/tv/{media_id}"
            
            params = tmdb.base_params.copy()
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
            
            backdrop_path = response.get("backdrop_path")
            if backdrop_path:
                backdrop_url = f"https://image.tmdb.org/t/p/w1280{backdrop_path}"
                # Cache for 7 days (same as TMDB metadata cache)
                cache.set(cache_key, backdrop_url, 60 * 60 * 24 * 7)
                return backdrop_url
        except Exception:
            pass
        
        # Cache the absence of backdrop to avoid repeated failed calls
        cache.set(cache_key, settings.IMG_NONE, 60 * 60 * 24)
        return settings.IMG_NONE
    
    def _get_igdb_backdrop(self, media_id):
        """Get widescreen backdrop image URL from IGDB for games.
        
        Prefers artworks (promotional images, typically widescreen) over screenshots.
        Uses caching to avoid repeated API calls for the same item.
        
        First tries to extract from the raw API response used by igdb.game(),
        then falls back to a lightweight API call if needed.
        """
        import logging
        logger = logging.getLogger(__name__)
        
        cache_key = f"igdb_backdrop_{media_id}"
        cached_backdrop = cache.get(cache_key)
        if cached_backdrop is not None:
            if cached_backdrop == settings.IMG_NONE:
                logger.debug(
                    "IGDB backdrop cache hit (no backdrop): game_id=%s, cache_key=%s",
                    media_id,
                    cache_key,
                )
            else:
                logger.debug(
                    "IGDB backdrop cache hit: game_id=%s, url=%s",
                    media_id,
                    cached_backdrop,
                )
            return cached_backdrop
        
        # Try to get artworks/screenshots from a fresh API call
        # We make our own call because igdb.game() caches processed data, not raw response
        try:
            from app.providers import igdb, services
            import requests
            
            logger.debug("IGDB backdrop cache miss for game_id=%s, making API request", media_id)
            
            # Make a lightweight request to get artwork image_ids and screenshot image_ids
            # We request both artworks.image_id (to get image_ids) and artworks (to get artwork IDs for fetching image_type)
            # Note: IGDB API doesn't support artworks.image_type as nested field,
            # so we'll fetch artworks separately to get image_type
            # Key Art is identified by image_type=4 in the artworks endpoint, not a separate field
            access_token = igdb.get_access_token()
            url = "https://api.igdb.com/v4/games"
            data = (
                "fields artworks,artworks.image_id,screenshots,screenshots.image_id;"
                f"where id = {media_id};"
            )
            headers = {
                "Client-ID": settings.IGDB_ID,
                "Authorization": f"Bearer {access_token}",
            }
            
            logger.debug(
                "Making IGDB API request for game %s: url=%s, fields=artworks,screenshots",
                media_id,
                url,
            )
            
            try:
                response = services.api_request(
                    Sources.IGDB.value,
                    "POST",
                    url,
                    data=data,
                    headers=headers,
                )
            except requests.exceptions.HTTPError as error:
                # Handle token refresh like igdb.game() does
                from app.providers.igdb import handle_error
                error_resp = handle_error(error)
                if error_resp and error_resp.get("retry"):
                    logger.debug("Retrying IGDB API request with new access token for game %s", media_id)
                    headers["Authorization"] = f"Bearer {igdb.get_access_token()}"
                    response = services.api_request(
                        Sources.IGDB.value,
                        "POST",
                        url,
                        data=data,
                        headers=headers,
                    )
                else:
                    raise
            
            logger.debug("IGDB API response for game %s: type=%s, length=%s", media_id, type(response), len(response) if response else 0)
            
            if response and len(response) > 0:
                game_response = response[0]
                logger.debug(
                    "IGDB game response for %s - keys: %s",
                    media_id,
                    list(game_response.keys()) if isinstance(game_response, dict) else None,
                )
                
                # Get artwork and screenshot data
                # When requesting artworks.image_id, IGDB returns a list of dicts with image_id
                # When requesting just artworks, IGDB returns a list of artwork IDs
                artworks_raw = game_response.get("artworks") or []
                screenshots_raw = game_response.get("screenshots") or []
                
                # Extract artwork IDs and image_ids
                artwork_ids = []  # Artwork record IDs for fetching details to get image_type
                artwork_image_ids = []  # Direct image_ids from nested field
                
                # Process artworks - check if we got nested data or just IDs
                if artworks_raw:
                    if isinstance(artworks_raw[0], dict):
                        # We got nested data with image_id (from artworks.image_id request)
                        for artwork in artworks_raw:
                            image_id = artwork.get("image_id")
                            artwork_id = artwork.get("id")
                            if image_id:
                                artwork_image_ids.append(image_id)
                            if artwork_id:
                                artwork_ids.append(artwork_id)
                    else:
                        # Just a list of artwork IDs (from artworks request)
                        artwork_ids = artworks_raw
                
                # Extract screenshot image_ids
                screenshot_ids = []
                if screenshots_raw:
                    if isinstance(screenshots_raw[0], dict):
                        screenshot_ids = [s.get("image_id") for s in screenshots_raw if s.get("image_id")]
                    else:
                        screenshot_ids = screenshots_raw
                
                logger.debug("IGDB artwork data for game %s: raw_count=%s, artwork_ids=%s, artwork_image_ids=%s", 
                           media_id, len(artworks_raw), artwork_ids, artwork_image_ids)
                logger.debug("IGDB screenshot IDs for game %s: count=%s, data=%s", media_id, len(screenshot_ids), screenshot_ids)
                
                # Fetch artwork details to get image_type (to identify Key Art)
                # Key Art is identified by image_type=4 in the artworks endpoint
                key_arts = []
                other_artworks = []
                
                # Fetch artwork details to get image_type (to identify Key Art)
                # Even if we have image_ids, we still need image_type to prioritize Key Art
                if artwork_ids:
                    # Fetch artwork details with image_type
                    # IGDB API uses "in" operator for multiple IDs: where id = (1,2,3);
                    artworks_url = "https://api.igdb.com/v4/artworks"
                    
                    # Process artworks in batches to avoid query length issues
                    batch_size = 50
                    all_artwork_details = []
                    
                    logger.debug("Fetching artwork details for game %s: %s artworks", media_id, len(artwork_ids))
                    
                    try:
                        for i in range(0, len(artwork_ids), batch_size):
                            batch_ids = artwork_ids[i:i + batch_size]
                            artwork_id_list = ','.join(str(aid) for aid in batch_ids)
                            # IGDB artwork types: 0=Other, 1=Box Art, 2=Screenshot, 3=Clear Logo, 4=Top Banner, 5=Marquee, 6=Steam Grid, 7=Hero, 8=Logo, 9=Icon
                            # Key Art is typically artwork_type=4 (Top Banner) or artwork_type=7 (Hero)
                            artworks_data = (
                                f"fields image_id,artwork_type;"
                                f"where id = ({artwork_id_list});"
                            )
                            
                            logger.debug("Fetching artwork details batch %s-%s for game %s", i+1, min(i+batch_size, len(artwork_ids)), media_id)
                            
                            try:
                                artworks_response = services.api_request(
                                    Sources.IGDB.value,
                                    "POST",
                                    artworks_url,
                                    data=artworks_data,
                                    headers=headers,
                                )
                                if artworks_response:
                                    all_artwork_details.extend(artworks_response)
                            except requests.exceptions.HTTPError as error:
                                # Log the actual error response for debugging
                                try:
                                    error_json = error.response.json()
                                    logger.warning(
                                        "IGDB artworks API error for game %s batch: %s",
                                        media_id,
                                        error_json,
                                    )
                                except:
                                    logger.warning(
                                        "IGDB artworks API error for game %s batch: %s",
                                        media_id,
                                        error,
                                    )
                                # Continue with other batches even if one fails
                                continue
                        
                        if all_artwork_details:
                            # Log all artwork types to help identify which is Key Art
                            artwork_types_found = {}
                            for artwork in all_artwork_details:
                                artwork_type = artwork.get("artwork_type")
                                if artwork_type is not None:
                                    artwork_types_found[artwork_type] = artwork_types_found.get(artwork_type, 0) + 1
                            logger.debug("Artwork types found for game %s: %s", media_id, artwork_types_found)
                            
                            for artwork in all_artwork_details:
                                image_id = artwork.get("image_id")
                                artwork_type = artwork.get("artwork_type")
                                if image_id:
                                    # IGDB artwork types: 0=Other, 1=Box Art, 2=Screenshot, 3=Clear Logo, 4=Top Banner, 5=Marquee, 6=Steam Grid, 7=Hero, 8=Logo, 9=Icon
                                    # Based on user feedback, artwork_type=4 might be Concept Art, not Key Art
                                    # Need to identify the correct type for Key Art - possibly type 7 (Hero) or a different value
                                    # For now, let's try type 7 (Hero) as Key Art, and if that doesn't work, we'll need to check the actual values
                                    if artwork_type == 7:  # Hero - trying this as Key Art
                                        key_arts.append(image_id)
                                        logger.debug("Identified Key Art (Hero) for game %s: image_id=%s, artwork_type=%s", media_id, image_id, artwork_type)
                                    elif artwork_type == 4:  # Top Banner - might be Concept Art based on user feedback
                                        # Skip type 4 for now since user says it's showing Concept Art
                                        other_artworks.append(image_id)
                                        logger.debug("Skipping Top Banner (might be Concept Art) for game %s: image_id=%s, artwork_type=%s", media_id, image_id, artwork_type)
                                    else:
                                        other_artworks.append(image_id)
                                        logger.debug("Other artwork for game %s: image_id=%s, artwork_type=%s", media_id, image_id, artwork_type)
                            
                            logger.debug("Separated artworks for game %s: Key Arts=%s, Other artworks=%s", media_id, len(key_arts), len(other_artworks))
                        else:
                            logger.warning("No artwork details returned for game %s", media_id)
                            # Fallback to using all artwork image_ids
                            if artwork_image_ids:
                                other_artworks = artwork_image_ids
                                logger.debug("Using artwork image_ids as fallback (no artwork details returned) for game %s", media_id)
                    except Exception as exc:
                        logger.warning(
                            "Failed to fetch artwork details for game %s: %s. Will try all artworks without filtering by type.",
                            media_id,
                            exc,
                            exc_info=True,
                        )
                        # Fallback: if we have image_ids but artwork details fetch failed,
                        # use them without type filtering (will still filter by aspect ratio)
                        if artwork_image_ids:
                            other_artworks = artwork_image_ids
                            logger.debug("Using artwork image_ids as fallback (artwork details fetch failed) for game %s", media_id)
                        else:
                            # No image_ids available, skip artworks
                            logger.debug("Skipping artworks for game %s due to failed artwork details fetch", media_id)
                elif artwork_image_ids:
                    # We have image_ids but no artwork IDs to fetch details
                    # This shouldn't happen if we requested both, but handle it gracefully
                    other_artworks = artwork_image_ids
                    logger.debug("Using artwork image_ids directly (no artwork IDs to fetch details) for game %s", media_id)
                
                # Process artworks with priority: Key Art first (no aspect ratio check), then filter other artworks by aspect ratio
                # First priority: Try Key Art (use directly without aspect ratio check)
                if key_arts:
                    logger.debug("Trying %s Key Art images for game %s", len(key_arts), media_id)
                    # Use first Key Art image directly - Key Art is designed to be widescreen
                    key_art_id = key_arts[0]
                    backdrop_url = (
                        f"https://images.igdb.com/igdb/image/upload/"
                        f"t_screenshot_big_2x/{key_art_id}.jpg"
                    )
                    logger.info(
                        "Found IGDB Key Art backdrop for game %s: %s",
                        media_id,
                        backdrop_url,
                    )
                    cache.set(cache_key, backdrop_url, 60 * 60 * 24 * 7)
                    return backdrop_url
                
                # Second priority: Try other artworks, filtering by aspect ratio
                if other_artworks:
                    logger.debug("Trying %s other artwork images for game %s", len(other_artworks), media_id)
                    for artwork_id in other_artworks:
                        backdrop_url = self._check_igdb_image_aspect_ratio(artwork_id, media_id, "artwork")
                        if backdrop_url:
                            # backdrop_url from _check_igdb_image_aspect_ratio is already the full URL
                            logger.info(
                                "Found IGDB artwork backdrop for game %s: %s",
                                media_id,
                                backdrop_url,
                            )
                            cache.set(cache_key, backdrop_url, 60 * 60 * 24 * 7)
                            return backdrop_url
                        else:
                            logger.debug("Artwork image_id=%s failed aspect ratio check for game %s", artwork_id, media_id)
                
                if key_arts or other_artworks:
                    logger.debug("No suitable artworks found (after aspect ratio filtering) for game %s. Key Arts tried: %s, Other artworks tried: %s", media_id, len(key_arts), len(other_artworks))
                
                # Third priority: Fall back to screenshots only if no suitable artworks found
                if screenshot_ids and len(screenshot_ids) > 0:
                    # Handle both list of dicts and list of image_ids
                    screenshot_image_id = None
                    if isinstance(screenshot_ids[0], dict):
                        screenshot_image_id = screenshot_ids[0].get("image_id")
                    else:
                        # If screenshots is a list of image_ids directly
                        screenshot_image_id = screenshot_ids[0]
                    
                    if screenshot_image_id:
                        # Use screenshot_big_2x size for widescreen background (high quality)
                        # Screenshots don't need aspect ratio filtering per user requirements
                        backdrop_url = (
                            f"https://images.igdb.com/igdb/image/upload/"
                            f"t_screenshot_big_2x/{screenshot_image_id}.jpg"
                        )
                        logger.info(
                            "Found IGDB screenshot backdrop for game %s: %s",
                            media_id,
                            backdrop_url,
                        )
                        # Cache for 7 days
                        cache.set(cache_key, backdrop_url, 60 * 60 * 24 * 7)
                        return backdrop_url
                    else:
                        logger.debug("First screenshot in list has no image_id for game %s", media_id)
                else:
                    logger.debug("No screenshots found for game %s", media_id)
                
                logger.warning(
                    "No artworks or screenshots found for IGDB game %s. Response keys: %s",
                    media_id,
                    list(game_response.keys()) if isinstance(game_response, dict) else None,
                )
            else:
                logger.warning("Empty response from IGDB API for game %s", media_id)
        except Exception as exc:
            logger.warning(
                "Failed to fetch IGDB backdrop for game %s: %s",
                media_id,
                exc,
                exc_info=True,
            )
        
        # Cache the absence of backdrop to avoid repeated failed calls
        logger.debug("Caching IMG_NONE for game %s (no backdrop found)", media_id)
        cache.set(cache_key, settings.IMG_NONE, 60 * 60 * 24)
        return settings.IMG_NONE
    
    def _check_igdb_image_aspect_ratio(self, image_id, media_id, image_type_label):
        """Check if an IGDB image has a suitable aspect ratio for list covers.
        
        Prefers 16:9 or closer to 3:2 (aspect ratio between 1.5 and 1.777).
        Skips images wider than 16:9 (aspect ratio > 1.777).
        
        Returns the backdrop URL if suitable, None otherwise.
        """
        import logging
        import requests
        from PIL import Image
        from io import BytesIO
        
        logger = logging.getLogger(__name__)
        
        try:
            # Fetch a small version of the image to check dimensions
            # For artworks, use t_cover_big_2x; for screenshots, use t_thumb
            # But since we don't know the type here, try t_cover_big_2x first (works for artworks)
            # If that fails, we'll catch the exception and return None
            image_url = f"https://images.igdb.com/igdb/image/upload/t_cover_big_2x/{image_id}.jpg"
            
            logger.debug(
                "Checking aspect ratio for %s image_id=%s: fetching %s",
                image_type_label,
                image_id,
                image_url,
            )
            
            # Fetch image with timeout
            response = requests.get(image_url, timeout=5, stream=True)
            if response.status_code == 404:
                # Try t_thumb as fallback (for screenshots)
                image_url = f"https://images.igdb.com/igdb/image/upload/t_thumb/{image_id}.jpg"
                response = requests.get(image_url, timeout=5, stream=True)
            response.raise_for_status()
            
            # Read image and check dimensions
            img_data = BytesIO(response.content)
            img = Image.open(img_data)
            width, height = img.size
            
            if width == 0 or height == 0:
                logger.debug(
                    "Invalid image dimensions for %s image_id=%s: %sx%s",
                    image_type_label,
                    image_id,
                    width,
                    height,
                )
                return None
            
            aspect_ratio = width / height
            logger.debug(
                "Image dimensions for %s image_id=%s: %sx%s, aspect_ratio=%.3f",
                image_type_label,
                image_id,
                width,
                height,
                aspect_ratio,
            )
            
            # Prefer 16:9 (1.777...) or closer to 3:2 (1.5)
            # Skip if wider than 16:9 (aspect_ratio > 1.778, allowing small rounding)
            # Accept if between 1.5 and 1.778 (inclusive)
            # 16:9 = 1.777777..., so we allow up to 1.778 to account for rounding
            if aspect_ratio > 1.778:
                logger.debug(
                    "Skipping %s image_id=%s: aspect ratio %.3f is wider than 16:9",
                    image_type_label,
                    image_id,
                    aspect_ratio,
                )
                return None
            
            if aspect_ratio < 1.5:
                logger.debug(
                    "Skipping %s image_id=%s: aspect ratio %.3f is narrower than 3:2",
                    image_type_label,
                    image_id,
                    aspect_ratio,
                )
                return None
            
            # Aspect ratio is suitable (between 1.5 and 1.777)
            # Use screenshot_big_2x for artworks (widescreen, high quality)
            # This size exists for both artworks and screenshots
            backdrop_url = (
                f"https://images.igdb.com/igdb/image/upload/"
                f"t_screenshot_big_2x/{image_id}.jpg"
            )
            logger.debug(
                "Accepting %s image_id=%s: aspect ratio %.3f is suitable (16:9 or closer to 3:2)",
                image_type_label,
                image_id,
                aspect_ratio,
            )
            return backdrop_url
            
        except Exception as exc:
            logger.debug(
                "Failed to check aspect ratio for %s image_id=%s: %s",
                image_type_label,
                image_id,
                exc,
            )
            # If we can't check aspect ratio, skip this image
            return None


class CustomListItemManager(models.Manager):
    """Manager for custom list items."""

    def get_last_added_date(self, custom_list):
        """Return the last time an item was added to a specific list."""
        try:
            return self.filter(custom_list=custom_list).latest("date_added").date_added
        except self.model.DoesNotExist:
            return None


class CustomListItem(models.Model):
    """Model for items in custom lists."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    custom_list = models.ForeignKey(CustomList, on_delete=models.CASCADE)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The user who added this item to the list",
    )
    date_added = models.DateTimeField(auto_now_add=True)

    objects = CustomListItemManager()

    class Meta:
        """Meta options for the model."""

        ordering = ["date_added"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "custom_list"],
                name="%(app_label)s_customlistitem_unique_item_list",
            ),
        ]

    def __str__(self):
        """Return the name of the list item."""
        return self.item.title


class ListRecommendation(models.Model):
    """Model for item recommendations to custom lists."""

    custom_list = models.ForeignKey(
        CustomList,
        on_delete=models.CASCADE,
        related_name="recommendations",
    )
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    recommended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The user who recommended this item (null if anonymous)",
    )
    anonymous_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Display name for anonymous recommenders",
    )
    note = models.TextField(
        blank=True,
        default="",
        help_text="Optional note from the recommender explaining their recommendation",
    )
    date_recommended = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-date_recommended"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "custom_list"],
                name="%(app_label)s_listrecommendation_unique_item_list",
            ),
        ]

    def __str__(self):
        """Return a string representation of the recommendation."""
        return f"{self.item.title} recommended for {self.custom_list.name}"

    @property
    def recommender_display_name(self):
        """Return the display name of the recommender."""
        if self.recommended_by:
            return self.recommended_by.username
        return self.anonymous_name or "Anonymous"


class ListActivityType(models.TextChoices):
    """Choices for list activity types."""

    ITEM_ADDED = "item_added", "Item Added"
    ITEM_REMOVED = "item_removed", "Item Removed"
    RECOMMENDATION_APPROVED = "recommendation_approved", "Recommendation Approved"
    RECOMMENDATION_DENIED = "recommendation_denied", "Recommendation Denied"
    LIST_CREATED = "list_created", "List Created"
    LIST_EDITED = "list_edited", "List Edited"
    COLLABORATOR_ADDED = "collaborator_added", "Collaborator Added"
    COLLABORATOR_REMOVED = "collaborator_removed", "Collaborator Removed"


class ListActivity(models.Model):
    """Model for tracking list activity history."""

    custom_list = models.ForeignKey(
        CustomList,
        on_delete=models.CASCADE,
        related_name="activities",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The user who performed this action",
    )
    activity_type = models.CharField(
        max_length=30,
        choices=ListActivityType.choices,
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="The item involved in this activity (if applicable)",
    )
    details = models.TextField(
        blank=True,
        default="",
        help_text="Additional details about the activity",
    )
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-timestamp"]
        verbose_name_plural = "List activities"

    def __str__(self):
        """Return a string representation of the activity."""
        return f"{self.get_activity_type_display()} - {self.custom_list.name}"

    @property
    def description(self):
        """Return a human-readable description of the activity."""
        user_name = self.user.username if self.user else "Someone"
        item_title = self.item.title if self.item else "an item"

        descriptions = {
            ListActivityType.ITEM_ADDED: f"{user_name} added {item_title}",
            ListActivityType.ITEM_REMOVED: f"{user_name} removed {item_title}",
            ListActivityType.RECOMMENDATION_APPROVED: f"{user_name} approved {item_title}",
            ListActivityType.RECOMMENDATION_DENIED: f"{user_name} denied {item_title}",
            ListActivityType.LIST_CREATED: f"{user_name} created the list",
            ListActivityType.LIST_EDITED: f"{user_name} edited the list",
            ListActivityType.COLLABORATOR_ADDED: f"{user_name} added a collaborator",
            ListActivityType.COLLABORATOR_REMOVED: f"{user_name} removed a collaborator",
        }
        base_desc = descriptions.get(self.activity_type, "Unknown activity")
        if self.details:
            return f"{base_desc}: {self.details}"
        return base_desc
