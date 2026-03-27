document.addEventListener("alpine:init", () => {
  Alpine.data("episodeBulkTrackForm", (domainId) => ({
    domain: null,
    firstSeason: "",
    firstEpisode: "",
    lastSeason: "",
    lastEpisode: "",
    writeMode: "add",
    distributionMode: "air_date",
    summaryText: "Choose the first and last episode to log a bulk play range.",
    rangeWarning: "",

    init() {
      const script = document.getElementById(domainId);
      if (!script) {
        return;
      }

      this.domain = JSON.parse(script.textContent);
      this.firstSeason =
        this.$refs.firstSeason.value ||
        String(this.domain.defaultFirst.season_number);
      this.lastSeason =
        this.$refs.lastSeason.value ||
        String(this.domain.defaultLast.season_number);
      this.firstEpisode =
        this.$refs.firstEpisode.dataset.currentValue ||
        String(this.domain.defaultFirst.episode_number);
      this.lastEpisode =
        this.$refs.lastEpisode.dataset.currentValue ||
        String(this.domain.defaultLast.episode_number);

      this.syncEpisodeOptions("first");
      this.syncEpisodeOptions("last");
      this.writeMode =
        this.$el.querySelector('[name="write_mode"]')?.value || "add";
      this.distributionMode =
        this.$el.querySelector('[name="distribution_mode"]')?.value ||
        "air_date";
      this.refreshSummary();
    },

    seasonEpisodes(seasonNumber) {
      if (!this.domain) {
        return [];
      }
      return this.domain.seasonEpisodeMap[String(seasonNumber)] || [];
    },

    pad(value) {
      return String(value).padStart(2, "0");
    },

    selectedEpisode(side) {
      const seasonNumber = side === "first" ? this.firstSeason : this.lastSeason;
      const episodeNumber = side === "first" ? this.firstEpisode : this.lastEpisode;

      return this.seasonEpisodes(seasonNumber).find(
        (episode) => String(episode.episode_number) === String(episodeNumber),
      );
    },

    selectedEpisodeAirDate(side) {
      return this.selectedEpisode(side)?.air_date || "";
    },

    timeSegment(input) {
      if (input?.value && input.value.includes("T")) {
        return input.value.split("T")[1].slice(0, 5);
      }

      const now = new Date();
      return `${this.pad(now.getHours())}:${this.pad(now.getMinutes())}`;
    },

    applyEpisodeAirDate(side, fieldRefName) {
      const airDate = this.selectedEpisodeAirDate(side);
      const input = this.$refs[fieldRefName];
      if (!airDate || !input) {
        return;
      }

      const datePart = airDate.slice(0, 10);
      if (input.type === "datetime-local") {
        input.value = `${datePart}T${this.timeSegment(input)}`;
      } else {
        input.value = datePart;
      }

      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    },

    syncEpisodeOptions(side) {
      const isFirst = side === "first";
      const select = isFirst ? this.$refs.firstEpisode : this.$refs.lastEpisode;
      const seasonNumber = isFirst ? this.firstSeason : this.lastSeason;
      const currentValue = isFirst ? this.firstEpisode : this.lastEpisode;
      const episodes = this.seasonEpisodes(seasonNumber);

      while (select.firstChild) {
        select.removeChild(select.firstChild);
      }

      episodes.forEach((episode) => {
        const option = document.createElement("option");
        option.value = String(episode.episode_number);
        option.textContent =
          episode.selector_label ||
          `E${episode.episode_number} - ${episode.episode_title}`;
        select.appendChild(option);
      });

      const hasCurrentValue = episodes.some(
        (episode) => String(episode.episode_number) === String(currentValue),
      );
      if (hasCurrentValue) {
        select.value = String(currentValue);
      } else if (episodes.length > 0) {
        select.value = String(
          isFirst
            ? episodes[0].episode_number
            : episodes[episodes.length - 1].episode_number,
        );
      }

      if (isFirst) {
        this.firstEpisode = select.value;
      } else {
        this.lastEpisode = select.value;
      }
      this.refreshSummary();
    },

    selectedRangeEpisodes() {
      if (!this.domain) {
        return [];
      }

      const firstEpisode = this.selectedEpisode("first");
      const lastEpisode = this.selectedEpisode("last");

      if (!firstEpisode || !lastEpisode) {
        return [];
      }

      const allEpisodes = Object.values(this.domain.seasonEpisodeMap).flat();
      return allEpisodes.filter(
        (episode) =>
          firstEpisode.order <= episode.order &&
          episode.order <= lastEpisode.order,
      );
    },

    refreshSummary() {
      const selectedEpisodes = this.selectedRangeEpisodes();
      if (selectedEpisodes.length === 0) {
        this.summaryText = "Choose a valid ordered episode range to log plays.";
        this.rangeWarning = "";
        return;
      }

      const existingPlayCount = selectedEpisodes.reduce(
        (total, episode) => total + (episode.existing_play_count || 0),
        0,
      );
      const verb =
        this.writeMode === "replace" ? "replace" : "add";
      const distributionLabel =
        this.distributionMode === "air_date"
          ? "target air dates within the selected date range"
          : "an even date range";

      this.summaryText =
        `This will ${verb} ${selectedEpisodes.length} ordered episode play` +
        `${selectedEpisodes.length === 1 ? "" : "s"} using ${distributionLabel}.`;

      if (this.distributionMode === "air_date") {
        const missingAirDates = selectedEpisodes.filter(
          (episode) => !episode.air_date,
        ).length;
        if (missingAirDates > 0) {
          this.rangeWarning =
            `${missingAirDates} selected episode` +
            `${missingAirDates === 1 ? " is" : "s are"} missing air dates.`;
          return;
        }
      }

      if (this.writeMode === "replace") {
        this.rangeWarning =
          existingPlayCount > 0
            ? `This will delete ${existingPlayCount} existing play` +
              `${existingPlayCount === 1 ? "" : "s"} in the selected range before adding the new ordered pass.`
            : "No existing plays are currently logged in the selected range.";
        return;
      }

      this.rangeWarning =
        existingPlayCount > 0
          ? `This range already has ${existingPlayCount} logged play` +
            `${existingPlayCount === 1 ? "" : "s"}. New plays will be appended in order.`
          : "";
    },
  }));
});
