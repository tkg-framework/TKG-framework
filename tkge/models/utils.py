import torch


def all_candidates_of_ent_queries(queries: torch.Tensor, vocab_size: int):
    """
    Generate all candidate tuples of the queries with absent entities.
    args:
        queries: entity prediction queries with either head or tail absent / value: float('nan')
            size: [query_num, query_dim]
        vocab_size: the vocabulary size of the dataset
    return:
        candidates: size [query_num * vocab_size, query_dim]
    """

    assert torch.isnan(queries).sum(1).byte().all(), "Either head or tail should be absent."

    dim_size = queries.size(1)

    missing_pos = torch.isnan(queries).nonzero()
    candidates = queries.repeat((1, vocab_size)).view(-1, dim_size)

    for p in missing_pos:
        candidates[p[0] * vocab_size:(p[0] + 1) * vocab_size, p[1]] = torch.arange(vocab_size)

    return candidates
