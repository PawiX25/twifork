# This token is common to all accounts and does not need to be changed.
TOKEN = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA'

DOMAIN = 'x.com'
# Hosts the clients authenticate against. `_ui_metrics` deliberately stays on
# twitter.com, so a cookie pinned only to x.com would never reach it. Shared so
# the logged-in and guest clients cannot drift apart on this.
COOKIE_DOMAINS = (f'.{DOMAIN}', '.twitter.com')

FEATURES = {
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'tweetypie_unmention_optimization_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'rweb_video_timestamps_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'responsive_web_media_download_video_enabled': False,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_enhance_cards_enabled': False,
    # Without this X silently omits `parody_commentary_fan_label` from every
    # user it returns - no error, the key is simply absent. Measured against
    # a labelled account: absent with the flag off, 'Fan' with it on. A
    # missing feature flag does not fail the request, it quietly trims the
    # response, which is why the gap went unnoticed.
    'profile_label_improvements_pcf_label_in_post_enabled': True
}

# Feature switches the AudioSpaceById query asks for. The web client pins the
# two `spaces_2022_h2_*` flags plus the standard FEATURES set; the rest are
# carried over from the current web bundle so the response shape stays
# identical to what the browser gets.
AUDIO_SPACE_FEATURES = {
    **FEATURES,
    'spaces_2022_h2_spaces_communities': True,
    'spaces_2022_h2_clipping': True,
    'responsive_web_grok_analyze_button_fetch_trends_enabled': True,
    'responsive_web_grok_analyze_post_followups_enabled': True,
    'responsive_web_grok_show_grok_translated_post': True,
    'responsive_web_grok_analysis_button_from_backend': True,
    'responsive_web_grok_image_annotation_enabled': True,
    'responsive_web_grok_imagine_annotation_enabled': True,
    'responsive_web_grok_community_note_auto_translation_is_enabled': True,
    'content_disclosure_indicator_enabled': True,
    'content_disclosure_ai_generated_indicator_enabled': True,
    'post_ctas_fetch_enabled': True,
    'rweb_cashtags_enabled': True,
}


USER_FEATURES = {
    'hidden_profile_likes_enabled': True,
    'hidden_profile_subscriptions_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'subscriptions_verification_info_is_identity_verified_enabled': True,
    'subscriptions_verification_info_verified_since_enabled': True,
    'highlights_tweets_tab_ui_enabled': True,
    'responsive_web_twitter_article_notes_tab_enabled': False,
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    # See the note in FEATURES: this is what makes X return the
    # parody/commentary/fan label on a profile lookup.
    'profile_label_improvements_pcf_label_in_post_enabled': True
}

CREATE_LIST_FEATURES = {
    # Captured from the web client on 2026-07-28. The list *management*
    # operations take this short set; the list *timeline* ones take the long
    # tweet-timeline set instead. Kept separate from LIST_FEATURES because the
    # other operations that constant serves still run on their old documents.
    'profile_label_improvements_pcf_label_in_post_enabled': True,
    'responsive_web_profile_redirect_enabled': True,
    'rweb_tipjar_consumption_enabled': False,
    'verified_phone_label_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True
}

LIST_FEATURES = {
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True
}

COMMUNITY_NOTE_FEATURES = {
    'responsive_web_birdwatch_media_notes_enabled': True,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'rweb_tipjar_consumption_enabled': False,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False
}

COMMUNITY_TWEETS_FEATURES = {
    'rweb_tipjar_consumption_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'tweetypie_unmention_optimization_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'creator_subscriptions_quote_tweet_preview_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'rweb_video_timestamps_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'responsive_web_enhance_cards_enabled': False
}

JOIN_COMMUNITY_FEATURES = {
    'rweb_tipjar_consumption_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True
}

NOTE_TWEET_FEATURES = {
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'tweetypie_unmention_optimization_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'creator_subscriptions_quote_tweet_preview_enabled': False,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'articles_preview_enabled': False,
    'rweb_video_timestamps_enabled': True,
    'rweb_tipjar_consumption_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'tweet_with_visibility_results_prefer_gql_media_interstitial_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_enhance_cards_enabled': False
}

SIMILAR_POSTS_FEATURES = {
    'rweb_tipjar_consumption_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'articles_preview_enabled': False,
    'tweetypie_unmention_optimization_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'creator_subscriptions_quote_tweet_preview_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'tweet_with_visibility_results_prefer_gql_media_interstitial_enabled': True,
    'rweb_video_timestamps_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'responsive_web_enhance_cards_enabled': False
}

BOOKMARK_FOLDER_TIMELINE_FEATURES = {
    'rweb_tipjar_consumption_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'articles_preview_enabled': False,
    'tweetypie_unmention_optimization_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'creator_subscriptions_quote_tweet_preview_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'tweet_with_visibility_results_prefer_gql_media_interstitial_enabled': True,
    'rweb_video_timestamps_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'responsive_web_enhance_cards_enabled': False
}

TWEET_RESULT_BY_REST_ID_FEATURES = {
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'articles_preview_enabled': True,
    'tweetypie_unmention_optimization_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'creator_subscriptions_quote_tweet_preview_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'rweb_video_timestamps_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'rweb_tipjar_consumption_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_enhance_cards_enabled': False
}

USER_HIGHLIGHTS_TWEETS_FEATURES = {
    'rweb_tipjar_consumption_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'articles_preview_enabled': True,
    'tweetypie_unmention_optimization_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'creator_subscriptions_quote_tweet_preview_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'rweb_video_timestamps_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    'responsive_web_enhance_cards_enabled': False
}

SEARCH_TIMELINE_FEATURES = {
    'rweb_video_screen_enabled': False,
    'rweb_cashtags_enabled': True,
    'profile_label_improvements_pcf_label_in_post_enabled': True,
    'responsive_web_profile_redirect_enabled': False,
    'rweb_tipjar_consumption_enabled': False,
    'verified_phone_label_enabled': False,
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'premium_content_api_read_enabled': False,
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'responsive_web_grok_analyze_button_fetch_trends_enabled': False,
    'responsive_web_grok_analyze_post_followups_enabled': True,
    'responsive_web_jetfuel_frame': True,
    'responsive_web_grok_share_attachment_enabled': True,
    'responsive_web_grok_annotations_enabled': True,
    'articles_preview_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'content_disclosure_indicator_enabled': True,
    'content_disclosure_ai_generated_indicator_enabled': True,
    'responsive_web_grok_show_grok_translated_post': True,
    'responsive_web_grok_analysis_button_from_backend': True,
    'post_ctas_fetch_enabled': True,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': False,
    'responsive_web_grok_image_annotation_enabled': True,
    'responsive_web_grok_imagine_annotation_enabled': True,
    'responsive_web_grok_community_note_auto_translation_is_enabled': True,
    'responsive_web_enhance_cards_enabled': False
}

TWEET_RESULTS_BY_REST_IDS_FEATURES = {
    'creator_subscriptions_tweet_preview_api_enabled': True,
    'premium_content_api_read_enabled': False,
    'communities_web_enable_tweet_community_results_fetch': True,
    'c9s_tweet_anatomy_moderator_badge_enabled': True,
    'responsive_web_grok_analyze_button_fetch_trends_enabled': False,
    'responsive_web_grok_analyze_post_followups_enabled': True,
    'responsive_web_grok_share_attachment_enabled': True,
    'articles_preview_enabled': True,
    'responsive_web_edit_tweet_api_enabled': True,
    'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
    'view_counts_everywhere_api_enabled': True,
    'longform_notetweets_consumption_enabled': True,
    'responsive_web_twitter_article_tweet_consumption_enabled': True,
    'tweet_awards_web_tipping_enabled': False,
    'creator_subscriptions_quote_tweet_preview_enabled': False,
    'freedom_of_speech_not_reach_fetch_enabled': True,
    'standardized_nudges_misinfo': True,
    'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
    'rweb_video_timestamps_enabled': True,
    'longform_notetweets_rich_text_read_enabled': True,
    'longform_notetweets_inline_media_enabled': True,
    # Was False here while every other set has it on, so tweets fetched by id
    # came back without the parody/commentary/fan label their authors carry.
    'profile_label_improvements_pcf_label_in_post_enabled': True,
    'rweb_tipjar_consumption_enabled': True,
    'responsive_web_graphql_exclude_directive_enabled': True,
    'verified_phone_label_enabled': False,
    'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
    'responsive_web_graphql_timeline_navigation_enabled': True,
    'responsive_web_enhance_cards_enabled': False
}

EXPLORE_PAGE_FEATURES = {
  "rweb_video_screen_enabled": False,
  "payments_enabled": False,
  "profile_label_improvements_pcf_label_in_post_enabled": True,
  "rweb_tipjar_consumption_enabled": True,
  "verified_phone_label_enabled": False,
  "responsive_web_graphql_timeline_navigation_enabled": True,
  "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
  "creator_subscriptions_tweet_preview_api_enabled": True,
  "premium_content_api_read_enabled": False,
  "communities_web_enable_tweet_community_results_fetch": True,
  "c9s_tweet_anatomy_moderator_badge_enabled": True,
  "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
  "responsive_web_grok_analyze_post_followups_enabled": True,
  "responsive_web_jetfuel_frame": True,
  "responsive_web_grok_share_attachment_enabled": True,
  "articles_preview_enabled": True,
  "responsive_web_edit_tweet_api_enabled": True,
  "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
  "view_counts_everywhere_api_enabled": True,
  "longform_notetweets_consumption_enabled": True,
  "responsive_web_twitter_article_tweet_consumption_enabled": True,
  "tweet_awards_web_tipping_enabled": False,
  "responsive_web_grok_show_grok_translated_post": False,
  "responsive_web_grok_analysis_button_from_backend": True,
  "creator_subscriptions_quote_tweet_preview_enabled": False,
  "freedom_of_speech_not_reach_fetch_enabled": True,
  "standardized_nudges_misinfo": True,
  "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
  "longform_notetweets_rich_text_read_enabled": True,
  "longform_notetweets_inline_media_enabled": True,
  "responsive_web_grok_image_annotation_enabled": True,
  "responsive_web_grok_imagine_annotation_enabled": True,
  "responsive_web_grok_community_note_auto_translation_is_enabled": False,
  "responsive_web_enhance_cards_enabled": False
}

# NOTE: you can fetch these using explore_page
TIMELINE_IDS = {
    'trending': 'VGltZWxpbmU6DAC2CwABAAAACHRyZW5kaW5nAAA=',
    'for-you': 'VGltZWxpbmU6DAC2CwABAAAAB2Zvcl95b3UAAA==',
    'news': 'VGltZWxpbmU6DAC2CwABAAAABG5ld3MAAA==',
    'sports': 'VGltZWxpbmU6DAC2CwABAAAABnNwb3J0cwAA',
    'entertainment': 'VGltZWxpbmU6DAC2CwABAAAADWVudGVydGFpbm1lbnQAAA==',
}
GENERIC_TIMELINE_FEATURES = {
  "rweb_video_screen_enabled": False,
  "payments_enabled": False,
  "profile_label_improvements_pcf_label_in_post_enabled": True,
  "rweb_tipjar_consumption_enabled": True,
  "verified_phone_label_enabled": False,
  "creator_subscriptions_tweet_preview_api_enabled": True,
  "responsive_web_graphql_timeline_navigation_enabled": True,
  "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
  "premium_content_api_read_enabled": False,
  "communities_web_enable_tweet_community_results_fetch": True,
  "c9s_tweet_anatomy_moderator_badge_enabled": True,
  "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
  "responsive_web_grok_analyze_post_followups_enabled": True,
  "responsive_web_jetfuel_frame": True,
  "responsive_web_grok_share_attachment_enabled": True,
  "articles_preview_enabled": True,
  "responsive_web_edit_tweet_api_enabled": True,
  "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
  "view_counts_everywhere_api_enabled": True,
  "longform_notetweets_consumption_enabled": True,
  "responsive_web_twitter_article_tweet_consumption_enabled": True,
  "tweet_awards_web_tipping_enabled": False,
  "responsive_web_grok_show_grok_translated_post": False,
  "responsive_web_grok_analysis_button_from_backend": True,
  "creator_subscriptions_quote_tweet_preview_enabled": False,
  "freedom_of_speech_not_reach_fetch_enabled": True,
  "standardized_nudges_misinfo": True,
  "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
  "longform_notetweets_rich_text_read_enabled": True,
  "longform_notetweets_inline_media_enabled": True,
  "responsive_web_grok_image_annotation_enabled": True,
  "responsive_web_grok_imagine_annotation_enabled": True,
  "responsive_web_grok_community_note_auto_translation_is_enabled": False,
  "responsive_web_enhance_cards_enabled": False
}
