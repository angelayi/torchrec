#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# pyre-strict

import copy
import unittest
from typing import Dict, List, Optional, Tuple

import torch
import torch.nn as nn
from hypothesis import given, settings, strategies as st
from torchrec.distributed.embedding import ShardedEmbeddingCollection
from torchrec.distributed.mc_embedding import (
    ManagedCollisionEmbeddingCollectionSharder,
    ShardedManagedCollisionEmbeddingCollection,
)
from torchrec.distributed.mc_modules import ShardedManagedCollisionCollection
from torchrec.distributed.shard import _shard_modules

from torchrec.distributed.sharding_plan import construct_module_sharding_plan, row_wise

from torchrec.distributed.test_utils.multi_process import (
    MultiProcessContext,
    MultiProcessTestBase,
)
from torchrec.distributed.types import ModuleSharder, ShardingEnv, ShardingPlan
from torchrec.modules.embedding_configs import EmbeddingConfig
from torchrec.modules.embedding_modules import EmbeddingCollection
from torchrec.modules.mc_embedding_modules import ManagedCollisionEmbeddingCollection
from torchrec.modules.mc_modules import (
    DistanceLFU_EvictionPolicy,
    ManagedCollisionCollection,
    MCHManagedCollisionModule,
)
from torchrec.optim.apply_optimizer_in_backward import apply_optimizer_in_backward
from torchrec.optim.rowwise_adagrad import RowWiseAdagrad
from torchrec.sparse.jagged_tensor import JaggedTensor, KeyedJaggedTensor
from torchrec.test_utils import skip_if_asan_class


class SparseArch(nn.Module):
    def __init__(
        self,
        tables: List[EmbeddingConfig],
        device: torch.device,
        return_remapped: bool = False,
    ) -> None:
        super().__init__()
        self._return_remapped = return_remapped

        mc_modules = {}
        mc_modules["table_0"] = MCHManagedCollisionModule(
            zch_size=(tables[0].num_embeddings),
            input_hash_size=4000,
            device=device,
            eviction_interval=2,
            eviction_policy=DistanceLFU_EvictionPolicy(),
        )

        mc_modules["table_1"] = MCHManagedCollisionModule(
            zch_size=(tables[1].num_embeddings),
            device=device,
            input_hash_size=4000,
            eviction_interval=2,
            eviction_policy=DistanceLFU_EvictionPolicy(),
        )

        self._mc_ec: ManagedCollisionEmbeddingCollection = (
            ManagedCollisionEmbeddingCollection(
                EmbeddingCollection(
                    tables=tables,
                    device=device,
                ),
                ManagedCollisionCollection(
                    managed_collision_modules=mc_modules,
                    # pyre-ignore
                    embedding_configs=tables,
                ),
                return_remapped_features=self._return_remapped,
            )
        )

    def forward(
        self, kjt: KeyedJaggedTensor
    ) -> Tuple[torch.Tensor, Optional[Dict[str, JaggedTensor]]]:
        if self._return_remapped:
            ec_out, remapped_ids_out = self._mc_ec(kjt)
        else:
            ec_out = self._mc_ec(kjt)
            remapped_ids_out = None

        pred = torch.cat(
            [ec_out[key].values() for key in ["feature_0", "feature_1"]],
            dim=1,
        )
        loss = pred.mean()
        return loss, remapped_ids_out


def _test_sharding(  # noqa C901
    tables: List[EmbeddingConfig],
    rank: int,
    world_size: int,
    sharder: ModuleSharder[nn.Module],
    backend: str,
    local_size: Optional[int] = None,
) -> None:
    with MultiProcessContext(rank, world_size, backend, local_size) as ctx:
        return_remapped: bool = True
        sparse_arch = SparseArch(
            tables,
            torch.device("meta"),
            return_remapped=return_remapped,
        )

        apply_optimizer_in_backward(
            RowWiseAdagrad,
            [
                sparse_arch._mc_ec._embedding_collection.embeddings["table_0"].weight,
                sparse_arch._mc_ec._embedding_collection.embeddings["table_1"].weight,
            ],
            {"lr": 0.01},
        )
        module_sharding_plan = construct_module_sharding_plan(
            sparse_arch._mc_ec,
            per_param_sharding={"table_0": row_wise(), "table_1": row_wise()},
            local_size=local_size,
            world_size=world_size,
            device_type="cuda" if torch.cuda.is_available() else "cpu",
            sharder=sharder,
        )

        sharded_sparse_arch = _shard_modules(
            module=copy.deepcopy(sparse_arch),
            plan=ShardingPlan({"_mc_ec": module_sharding_plan}),
            # pyre-fixme[6]: For 1st argument expected `ProcessGroup` but got
            #  `Optional[ProcessGroup]`.
            env=ShardingEnv.from_process_group(ctx.pg),
            sharders=[sharder],
            device=ctx.device,
        )

        assert isinstance(
            sharded_sparse_arch._mc_ec, ShardedManagedCollisionEmbeddingCollection
        )
        assert isinstance(
            sharded_sparse_arch._mc_ec._managed_collision_collection,
            ShardedManagedCollisionCollection,
        )


def _test_sharding_and_remapping(  # noqa C901
    tables: List[EmbeddingConfig],
    rank: int,
    world_size: int,
    kjt_input_per_rank: List[KeyedJaggedTensor],
    kjt_out_per_iter_per_rank: List[List[KeyedJaggedTensor]],
    initial_state_per_rank: List[Dict[str, torch.Tensor]],
    final_state_per_rank: List[Dict[str, torch.Tensor]],
    sharder: ModuleSharder[nn.Module],
    backend: str,
    local_size: Optional[int] = None,
) -> None:

    with MultiProcessContext(rank, world_size, backend, local_size) as ctx:
        kjt_input = kjt_input_per_rank[rank].to(ctx.device)
        kjt_out_per_iter = [
            kjt[rank].to(ctx.device) for kjt in kjt_out_per_iter_per_rank
        ]
        return_remapped: bool = True
        sparse_arch = SparseArch(
            tables,
            torch.device("meta"),
            return_remapped=return_remapped,
        )

        apply_optimizer_in_backward(
            RowWiseAdagrad,
            [
                sparse_arch._mc_ec._embedding_collection.embeddings["table_0"].weight,
                sparse_arch._mc_ec._embedding_collection.embeddings["table_1"].weight,
            ],
            {"lr": 0.01},
        )
        module_sharding_plan = construct_module_sharding_plan(
            sparse_arch._mc_ec,
            per_param_sharding={"table_0": row_wise(), "table_1": row_wise()},
            local_size=local_size,
            world_size=world_size,
            device_type="cuda" if torch.cuda.is_available() else "cpu",
            sharder=sharder,
        )

        sharded_sparse_arch = _shard_modules(
            module=copy.deepcopy(sparse_arch),
            plan=ShardingPlan({"_mc_ec": module_sharding_plan}),
            # pyre-fixme[6]: For 1st argument expected `ProcessGroup` but got
            #  `Optional[ProcessGroup]`.
            env=ShardingEnv.from_process_group(ctx.pg),
            sharders=[sharder],
            device=ctx.device,
        )

        assert isinstance(
            sharded_sparse_arch._mc_ec, ShardedManagedCollisionEmbeddingCollection
        )
        assert isinstance(
            sharded_sparse_arch._mc_ec._embedding_collection,
            ShardedEmbeddingCollection,
        )
        assert (
            sharded_sparse_arch._mc_ec._embedding_collection._has_uninitialized_input_dist
            is False
        )
        assert (
            not hasattr(
                sharded_sparse_arch._mc_ec._embedding_collection, "_input_dists"
            )
            or len(sharded_sparse_arch._mc_ec._embedding_collection._input_dists) == 0
        )

        assert isinstance(
            sharded_sparse_arch._mc_ec._managed_collision_collection,
            ShardedManagedCollisionCollection,
        )

        initial_state_dict = sharded_sparse_arch.state_dict()
        for key, sharded_tensor in initial_state_dict.items():
            postfix = ".".join(key.split(".")[-2:])
            if postfix in initial_state_per_rank[ctx.rank]:
                tensor = sharded_tensor.local_shards()[0].tensor.cpu()
                assert torch.equal(
                    tensor, initial_state_per_rank[ctx.rank][postfix]
                ), f"initial state {key} on {ctx.rank} does not match, got {tensor}, expect {initial_state_per_rank[rank][postfix]}"

        sharded_sparse_arch.load_state_dict(initial_state_dict)

        # sharded model
        # each rank gets a subbatch
        loss1, remapped_ids1 = sharded_sparse_arch(kjt_input)
        loss1.backward()
        loss2, remapped_ids2 = sharded_sparse_arch(kjt_input)
        loss2.backward()

        final_state_dict = sharded_sparse_arch.state_dict()
        for key, sharded_tensor in final_state_dict.items():
            postfix = ".".join(key.split(".")[-2:])
            if postfix in final_state_per_rank[ctx.rank]:
                tensor = sharded_tensor.local_shards()[0].tensor.cpu()
                assert torch.equal(
                    tensor, final_state_per_rank[ctx.rank][postfix]
                ), f"initial state {key} on {ctx.rank} does not match, got {tensor}, expect {initial_state_per_rank[rank][postfix]}"

        remapped_ids = [remapped_ids1, remapped_ids2]
        for key in kjt_input.keys():
            for i, kjt_out in enumerate(kjt_out_per_iter):
                assert torch.equal(
                    remapped_ids[i][key].values(),
                    kjt_out[key].values(),
                ), f"feature {key} on {ctx.rank} iteration {i} does not match, got {remapped_ids[i][key].values()}, expect {kjt_out[key].values()}"

        # TODO: validate embedding rows, and eviction


@skip_if_asan_class
class ShardedMCEmbeddingCollectionParallelTest(MultiProcessTestBase):
    @unittest.skipIf(
        torch.cuda.device_count() <= 1,
        "Not enough GPUs, this test requires at least two GPUs",
    )
    # pyre-ignore
    @given(backend=st.sampled_from(["nccl"]))
    @settings(deadline=20000)
    def test_uneven_sharding(self, backend: str) -> None:
        WORLD_SIZE = 2

        embedding_config = [
            EmbeddingConfig(
                name="table_0",
                feature_names=["feature_0"],
                embedding_dim=8,
                num_embeddings=17,
            ),
            EmbeddingConfig(
                name="table_1",
                feature_names=["feature_1"],
                embedding_dim=8,
                num_embeddings=33,
            ),
        ]

        self._run_multi_process_test(
            callable=_test_sharding,
            world_size=WORLD_SIZE,
            tables=embedding_config,
            sharder=ManagedCollisionEmbeddingCollectionSharder(),
            backend=backend,
        )

    @unittest.skipIf(
        torch.cuda.device_count() <= 1,
        "Not enough GPUs, this test requires at least two GPUs",
    )
    # pyre-ignore
    @given(backend=st.sampled_from(["nccl"]))
    @settings(deadline=20000)
    def test_even_sharding(self, backend: str) -> None:
        WORLD_SIZE = 2

        embedding_config = [
            EmbeddingConfig(
                name="table_0",
                feature_names=["feature_0"],
                embedding_dim=8,
                num_embeddings=16,
            ),
            EmbeddingConfig(
                name="table_1",
                feature_names=["feature_1"],
                embedding_dim=8,
                num_embeddings=32,
            ),
        ]

        self._run_multi_process_test(
            callable=_test_sharding,
            world_size=WORLD_SIZE,
            tables=embedding_config,
            sharder=ManagedCollisionEmbeddingCollectionSharder(),
            backend=backend,
        )

    @unittest.skipIf(
        torch.cuda.device_count() <= 1,
        "Not enough GPUs, this test requires at least two GPUs",
    )
    # pyre-ignore
    @given(backend=st.sampled_from(["nccl"]))
    @settings(deadline=None)
    def test_sharding_zch_mc_ec(self, backend: str) -> None:

        WORLD_SIZE = 2

        embedding_config = [
            EmbeddingConfig(
                name="table_0",
                feature_names=["feature_0"],
                embedding_dim=8,
                num_embeddings=16,
            ),
            EmbeddingConfig(
                name="table_1",
                feature_names=["feature_1"],
                embedding_dim=8,
                num_embeddings=32,
            ),
        ]

        kjt_input_per_rank = [  # noqa
            KeyedJaggedTensor.from_lengths_sync(
                keys=["feature_0", "feature_1"],
                values=torch.LongTensor(
                    [1000, 2000, 1001, 2000, 2001, 2002],
                ),
                lengths=torch.LongTensor([1, 1, 1, 1, 1, 1]),
                weights=None,
            ),
            KeyedJaggedTensor.from_lengths_sync(
                keys=["feature_0", "feature_1"],
                values=torch.LongTensor(
                    [
                        1000,
                        1002,
                        1004,
                        2000,
                        2002,
                        2004,
                    ],
                ),
                lengths=torch.LongTensor([1, 1, 1, 1, 1, 1]),
                weights=None,
            ),
        ]

        kjt_out_per_iter_per_rank: List[List[KeyedJaggedTensor]] = []
        kjt_out_per_iter_per_rank.append(
            [
                KeyedJaggedTensor.from_lengths_sync(
                    keys=["feature_0", "feature_1"],
                    values=torch.LongTensor(
                        [7, 15, 7, 31, 31, 31],
                    ),
                    lengths=torch.LongTensor([1, 1, 1, 1, 1, 1]),
                    weights=None,
                ),
                KeyedJaggedTensor.from_lengths_sync(
                    keys=["feature_0", "feature_1"],
                    values=torch.LongTensor(
                        [7, 7, 7, 31, 31, 31],
                    ),
                    lengths=torch.LongTensor([1, 1, 1, 1, 1, 1]),
                    weights=None,
                ),
            ]
        )
        # TODO: cleanup sorting so more dedugable/logical initial fill

        kjt_out_per_iter_per_rank.append(
            [
                KeyedJaggedTensor.from_lengths_sync(
                    keys=["feature_0", "feature_1"],
                    values=torch.LongTensor(
                        [3, 14, 4, 27, 29, 28],
                    ),
                    lengths=torch.LongTensor([1, 1, 1, 1, 1, 1]),
                    weights=None,
                ),
                KeyedJaggedTensor.from_lengths_sync(
                    keys=["feature_0", "feature_1"],
                    values=torch.LongTensor(
                        [3, 5, 6, 27, 28, 30],
                    ),
                    lengths=torch.LongTensor([1, 1, 1, 1, 1, 1]),
                    weights=None,
                ),
            ]
        )

        initial_state_per_rank = [
            {
                "table_0._mch_remapped_ids_mapping": torch.arange(8, dtype=torch.int64),
                "table_1._mch_remapped_ids_mapping": torch.arange(
                    16, dtype=torch.int64
                ),
            },
            {
                "table_0._mch_remapped_ids_mapping": torch.arange(
                    start=8, end=16, dtype=torch.int64
                ),
                "table_1._mch_remapped_ids_mapping": torch.arange(
                    start=16, end=32, dtype=torch.int64
                ),
            },
        ]
        max_int = torch.iinfo(torch.int64).max

        final_state_per_rank = [
            {
                "table_0._mch_sorted_raw_ids": torch.LongTensor(
                    [1000, 1001, 1002, 1004] + [max_int] * 4
                ),
                "table_1._mch_sorted_raw_ids": torch.LongTensor([max_int] * 16),
                "table_0._mch_remapped_ids_mapping": torch.LongTensor(
                    [3, 4, 5, 6, 0, 1, 2, 7]
                ),
                "table_1._mch_remapped_ids_mapping": torch.arange(
                    16, dtype=torch.int64
                ),
            },
            {
                "table_0._mch_sorted_raw_ids": torch.LongTensor([2000] + [max_int] * 7),
                "table_1._mch_sorted_raw_ids": torch.LongTensor(
                    [2000, 2001, 2002, 2004] + [max_int] * 12
                ),
                "table_0._mch_remapped_ids_mapping": torch.LongTensor(
                    [14, 8, 9, 10, 11, 12, 13, 15]
                ),
                "table_1._mch_remapped_ids_mapping": torch.LongTensor(
                    [27, 29, 28, 30, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 31]
                ),
            },
        ]

        self._run_multi_process_test(
            callable=_test_sharding_and_remapping,
            world_size=WORLD_SIZE,
            tables=embedding_config,
            kjt_input_per_rank=kjt_input_per_rank,
            kjt_out_per_iter_per_rank=kjt_out_per_iter_per_rank,
            initial_state_per_rank=initial_state_per_rank,
            final_state_per_rank=final_state_per_rank,
            sharder=ManagedCollisionEmbeddingCollectionSharder(),
            backend=backend,
        )
