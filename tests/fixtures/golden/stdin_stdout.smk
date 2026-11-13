rule all:
    input:
        "piped-1-out",
rule piped_1:
    input:
        i_0="in-1-out",
    output:
        o_0="piped-1-out",
    shell:
        "./t < {input.i_0:q} > {output.o_0:q}"