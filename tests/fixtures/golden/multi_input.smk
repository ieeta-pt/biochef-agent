rule all:
    input:
        "pair-1-out",
rule pair_1:
    input:
        i_0="in-1-out",
        i_1="in-2-out",
    output:
        o_0="pair-1-out",
    shell:
        "./t -a {input.i_0:q} -o {output.o_0:q} {input.i_1:q}"