import sys

from FastAutoAugment.nas import vis_genotype as vis


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise ValueError("usage:\n python {} GENOTYPE".format(sys.argv[0]))

    vis.draw_genotype(sys.argv[1], genotype_attr="normal")
