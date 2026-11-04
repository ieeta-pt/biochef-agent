rule all:
    input:
        "unflagged-1-out",
rule unflagged_1:
    input:
        i_0="in-1-out",
    output:
        o_0="unflagged-1-out",
    shell:
        "./t {input.i_0:q} {output.o_0:q}\ncp result.txt {output.o_0:q}"