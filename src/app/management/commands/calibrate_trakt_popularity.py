"""Evaluate the local Trakt popularity calibration fixture."""

from __future__ import annotations

import math

from django.core.management.base import BaseCommand

from app.services import trakt_popularity


class Command(BaseCommand):
    """Evaluate or brute-force local Trakt popularity parameters."""

    help = "Evaluate the checked-in Trakt popularity calibration fixture"

    def add_arguments(self, parser):
        parser.add_argument(
            "--grid-search",
            action="store_true",
            help="Search a parameter grid and report the best result",
        )
        parser.add_argument(
            "--no-table",
            action="store_true",
            help="Suppress the per-item rank table (show summary only)",
        )

    def handle(self, *_args, **options):
        if options["grid_search"]:
            self._handle_grid_search()
            return

        metrics = trakt_popularity.evaluate_calibration_fixture()
        if not options["no_table"]:
            self._print_rank_table(metrics)
        self.stdout.write(
            self.style.SUCCESS(
                f"\nitems={metrics['count']} "
                f"mae={metrics['mae']:.2f} "
                f"max_abs_error={metrics['max_abs_error']} "
                f"top_ten_overlap={metrics['top_ten_overlap']}",
            ),
        )

    def _print_rank_table(self, metrics):
        items = sorted(metrics["items"], key=lambda item: item["predicted_rank"])
        header = f"{'Pred':>4}  {'Exp':>4}  {'Err':>4}  {'Score':>8}  Title"
        self.stdout.write(header)
        self.stdout.write("-" * 70)
        for item in items:
            pred = item["predicted_rank"]
            exp = item["expected_rank"]
            err = pred - exp
            score = item.get("score") or 0.0
            sign = "+" if err > 0 else ""
            self.stdout.write(
                f"{pred:>4}  {exp:>4}  {sign}{err:>3}  {score:>8.1f}  {item['title']}",
            )

    def _handle_grid_search(self):
        fixture = trakt_popularity.load_calibration_fixture()
        items = fixture.get("items") or []

        best_result = None
        for prior_mean in (60.0, 65.0, 70.0, 75.0, 80.0):
            for prior_votes in (500.0, 1_000.0, 5_000.0, 10_000.0, 25_000.0, 50_000.0):
                for vote_offset in (1.0, 10.0, 100.0):
                    for vote_exponent in (1.0, 1.5, 2.0, 2.5, 3.0):
                        scored = []
                        for item in items:
                            score = trakt_popularity.compute_popularity_score(
                                item.get("rating"),
                                item.get("votes"),
                                prior_mean=prior_mean,
                                prior_votes=prior_votes,
                                vote_offset=vote_offset,
                                vote_exponent=vote_exponent,
                            )
                            scored.append((score or 0.0, item["title"], item["expected_rank"]))

                        predicted = sorted(
                            scored,
                            key=lambda entry: (-entry[0], entry[1].lower()),
                        )
                        predicted_ranks = {
                            title: index
                            for index, (_score, title, _rank) in enumerate(predicted, start=1)
                        }
                        absolute_errors = [
                            abs(predicted_ranks[title] - expected_rank)
                            for _score, title, expected_rank in scored
                        ]
                        mae = sum(absolute_errors) / len(absolute_errors) if absolute_errors else math.inf
                        result = (
                            mae,
                            max(absolute_errors) if absolute_errors else math.inf,
                            prior_mean,
                            prior_votes,
                            vote_offset,
                            vote_exponent,
                        )
                        if best_result is None or result < best_result:
                            best_result = result

        if best_result is None:
            self.stdout.write(self.style.ERROR("No calibration data available."))
            return

        mae, max_abs_error, prior_mean, prior_votes, vote_offset, vote_exponent = best_result
        self.stdout.write(
            self.style.SUCCESS(
                f"best_mae={mae:.2f} max_abs_error={max_abs_error} "
                f"prior_mean={prior_mean:.1f} prior_votes={prior_votes:.0f} "
                f"vote_offset={vote_offset:.1f} vote_exponent={vote_exponent:.1f}",
            ),
        )
